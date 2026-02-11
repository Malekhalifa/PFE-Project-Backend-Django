"""
CSV cleaning service — extracted from backend-python/worker_routes.py.

Pure Python module (no Django/FastAPI dependencies) that performs
configurable CSV cleaning operations:
  - Drop fully-empty rows (always)
  - Strip whitespace from string columns (always)
  - Remove duplicates
  - Handle missing values (drop / mean / median / mode)
  - Outlier handling (remove / cap / ignore)
  - Min-max normalization

Functions:
  - clean_csv(file_path, job_id, mode, rules, options) -> dict
"""
import os
from typing import Any

import pandas as pd


# ──────────────────────────────────────────────
# Default rules for auto mode
# ──────────────────────────────────────────────

DEFAULT_AUTO_RULES = {
    'remove_duplicates': True,
    'handle_missing': 'drop',
    'outliers': 'remove',
    'normalize': False,
}


def clean_csv(
    file_path: str,
    job_id: str,
    mode: str = 'auto',
    rules: dict | None = None,
    options: dict | None = None,
) -> dict[str, Any]:
    """
    Clean a CSV file according to the given rules.

    Args:
        file_path: Path to the CSV file on disk.
        job_id: Job ID (used for naming the cleaned output file).
        mode: "auto" (sensible defaults) or "manual" (caller-specified rules).
        rules: Cleaning rules dict with keys:
            - remove_duplicates (bool)
            - handle_missing (str): "drop" | "mean" | "median" | "mode"
            - outliers (str): "remove" | "cap" | "ignore"
            - normalize (bool)
        options: Extra options (reserved for future use).

    Returns:
        {
            "cleaned_file_path": str,
            "mode": str,
            "rules_applied": list[dict],
            "summary": { "rows_before": int, "rows_after": int, "rows_removed": int }
        }

    Raises:
        FileNotFoundError: If the input file does not exist.
        Exception: If CSV processing fails.
    """
    if not os.path.exists(file_path):
        raise FileNotFoundError(f'File not found: {file_path}')

    df = pd.read_csv(file_path)
    rows_before = len(df)
    rules_applied = []

    # Resolve rules: auto mode uses sensible defaults
    r = DEFAULT_AUTO_RULES.copy() if mode == 'auto' else (rules or {})
    remove_duplicates = r.get('remove_duplicates', False)
    handle_missing = r.get('handle_missing', '')
    outliers_mode = r.get('outliers', 'ignore')
    normalize = r.get('normalize', False)

    # ── Always: drop fully-empty rows ──
    before = len(df)
    df = df.dropna(how='all')
    removed = before - len(df)
    if removed > 0:
        rules_applied.append({
            'rule': 'drop_empty_rows',
            'description': 'Removed rows where every column is empty',
            'rows_removed': removed,
        })

    # ── Always: strip whitespace from string columns ──
    str_cols = df.select_dtypes(include=['object']).columns
    if len(str_cols) > 0:
        df[str_cols] = df[str_cols].apply(lambda c: c.str.strip())
        rules_applied.append({
            'rule': 'strip_whitespace',
            'description': f'Stripped leading/trailing whitespace from {len(str_cols)} text columns',
            'columns': list(str_cols),
        })

    # ── Rule: remove_duplicates ──
    if remove_duplicates:
        before = len(df)
        df = df.drop_duplicates()
        removed = before - len(df)
        if removed > 0:
            rules_applied.append({
                'rule': 'remove_duplicates',
                'description': 'Removed exact duplicate rows',
                'rows_removed': removed,
            })

    # ── Rule: handle_missing ──
    if handle_missing == 'drop':
        before = len(df)
        df = df.dropna()
        removed = before - len(df)
        if removed > 0:
            rules_applied.append({
                'rule': 'handle_missing_drop',
                'description': 'Dropped rows containing any missing value',
                'rows_removed': removed,
            })
    elif handle_missing in ('mean', 'median', 'mode'):
        numeric_cols = df.select_dtypes(include=['number']).columns
        non_numeric_cols = df.columns.difference(numeric_cols)
        filled_cols = []

        if handle_missing == 'mean' and len(numeric_cols) > 0:
            fills = df[numeric_cols].mean()
            df[numeric_cols] = df[numeric_cols].fillna(fills)
            filled_cols = list(numeric_cols[df[numeric_cols].notna().all()])
        elif handle_missing == 'median' and len(numeric_cols) > 0:
            fills = df[numeric_cols].median()
            df[numeric_cols] = df[numeric_cols].fillna(fills)
            filled_cols = list(numeric_cols[df[numeric_cols].notna().all()])
        elif handle_missing == 'mode':
            for col in df.columns:
                mode_val = df[col].mode()
                if not mode_val.empty:
                    df[col] = df[col].fillna(mode_val.iloc[0])
            filled_cols = list(df.columns)

        # For mean/median, fill non-numeric with mode as fallback
        if handle_missing in ('mean', 'median') and len(non_numeric_cols) > 0:
            for col in non_numeric_cols:
                mode_val = df[col].mode()
                if not mode_val.empty:
                    df[col] = df[col].fillna(mode_val.iloc[0])

        rules_applied.append({
            'rule': f'handle_missing_{handle_missing}',
            'description': f'Filled missing values using {handle_missing} strategy',
            'columns_affected': filled_cols[:20],
        })

    # ── Rule: outliers ──
    if outliers_mode in ('remove', 'cap'):
        numeric_cols = df.select_dtypes(include=['number']).columns
        if len(numeric_cols) > 0:
            means = df[numeric_cols].mean()
            stds = df[numeric_cols].std()

            if outliers_mode == 'remove':
                before = len(df)
                z_scores = ((df[numeric_cols] - means) / stds).abs()
                z_scores = z_scores.fillna(0).replace(
                    [float('inf'), float('-inf')], 0
                )
                outlier_mask = (z_scores > 3).any(axis=1)
                df = df[~outlier_mask]
                removed = before - len(df)
                if removed > 0:
                    rules_applied.append({
                        'rule': 'outliers_remove',
                        'description': f'Removed {removed} rows with z-score > 3 on any numeric column',
                        'rows_removed': removed,
                    })

            elif outliers_mode == 'cap':
                capped_cols = []
                for col in numeric_cols:
                    std = stds[col]
                    mean = means[col]
                    if std > 0:
                        lower = mean - 3 * std
                        upper = mean + 3 * std
                        before_cap = df[col].copy()
                        df[col] = df[col].clip(lower=lower, upper=upper)
                        if not df[col].equals(before_cap):
                            capped_cols.append(col)
                if capped_cols:
                    rules_applied.append({
                        'rule': 'outliers_cap',
                        'description': f'Capped values to +/-3 sigma on {len(capped_cols)} numeric columns',
                        'columns': capped_cols[:20],
                    })

    # ── Rule: normalize ──
    if normalize:
        numeric_cols = df.select_dtypes(include=['number']).columns
        if len(numeric_cols) > 0:
            mins = df[numeric_cols].min()
            maxs = df[numeric_cols].max()
            ranges = maxs - mins
            norm_cols = [c for c in numeric_cols if ranges[c] > 0]
            if norm_cols:
                df[norm_cols] = (df[norm_cols] - mins[norm_cols]) / ranges[norm_cols]
                rules_applied.append({
                    'rule': 'normalize',
                    'description': f'Min-max normalized {len(norm_cols)} numeric columns to [0, 1]',
                    'columns': norm_cols[:20],
                })

    rows_after = len(df)

    # Write cleaned CSV
    output_dir = os.path.dirname(file_path)
    cleaned_path = os.path.join(output_dir, f'{job_id}-cleaned.csv')
    df.to_csv(cleaned_path, index=False)

    return {
        'cleaned_file_path': cleaned_path,
        'mode': mode,
        'rules_applied': rules_applied,
        'summary': {
            'rows_before': rows_before,
            'rows_after': rows_after,
            'rows_removed': rows_before - rows_after,
        },
    }
