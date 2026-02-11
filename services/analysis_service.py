import io
import re
from typing import Dict, Any,List

import numpy as np
import pandas as pd
from sklearn.ensemble import IsolationForest
import math

_NAME_PATTERN = re.compile(r"^[A-Za-z]+(?:\s[A-Za-z]+)*$")

_EMAIL_PATTERN = re.compile(
    r"^[A-Za-z0-9._%+-]+@"        # local part (letters, digits, ., _, %, +, -)
    r"[A-Za-z0-9.-]+\."           # domain + dot
    r"[A-Za-z]{2,}$"              # top-level domain (at least 2 letters)
)

_DATE_PATTERN = re.compile(
    r"^("
    r"\d{4}[-/](0[1-9]|1[0-2])[-/](0[1-9]|[12]\d|3[01])|"        # YYYY-MM-DD or YYYY/MM/DD
    r"(0[1-9]|[12]\d|3[01])[-/](0[1-9]|1[0-2])[-/]\d{4}|"        # DD-MM-YYYY or DD/MM/YYYY
    r"(0[1-9]|1[0-2])[-/](0[1-9]|[12]\d|3[01])[-/]\d{4}"         # MM-DD-YYYY or MM/DD/YYYY
    r")$"
)

_IDENTIFIER_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_\-]{4,}$")


def load_dataframe(content: bytes) -> pd.DataFrame:
    try:
        return pd.read_csv(io.BytesIO(content))
    except pd.errors.EmptyDataError:
        return pd.DataFrame()


def compute_basic_quality(df: pd.DataFrame) -> tuple[float, float, int]:
    rows, cols = df.shape
    total = rows * cols

    missing_rate = df.isna().sum().sum() / total if total else 0.0
    duplicate_count = int(df.duplicated().sum())
    duplicate_rate = duplicate_count / rows if rows else 0.0

    return missing_rate, duplicate_rate, duplicate_count


def validate_series(series: pd.Series, rule: str) -> tuple[int, int]:
    s = series.fillna("").astype(str).str.strip()  # replace NaN with ""
    if s.empty:
        return 0, 0

    if rule == "email":
        valid_mask = s.str.fullmatch(_EMAIL_PATTERN).fillna(False)
    elif rule == "name":
        valid_mask = s.str.fullmatch(_NAME_PATTERN).fillna(False)
    elif rule == "id":
        valid_mask = s.str.fullmatch(_IDENTIFIER_PATTERN).fillna(False)
    else:
        return 0, 0

    valid = int(valid_mask.sum())
    invalid = int(len(s) - valid)  # total minus valid = invalid
    return valid, invalid


def compute_type_consistency(df: pd.DataFrame) -> dict:
    type_consistency = {}
    rows = len(df)

    for col in df.columns:
        s = df[col].fillna("").astype(str).str.strip()

        missing = int((s == "").sum())
        non_missing = rows - missing

        rule = infer_column_rule(col)

        valid = invalid = 0

        if rule:
            valid, invalid = validate_series(s, rule)
        else:
            # For non-rule columns, treat all non-empty rows as valid
            valid = non_missing
            invalid = 0

        type_consistency[col] = {
            "missing": missing,
            "valid": valid,
            "invalid": invalid,
            "total": non_missing,
            "rule": rule or "none",
        }

    return type_consistency


def compute_outlier_rate(numeric_df: pd.DataFrame) -> float:
    numeric_only = numeric_df.dropna(axis=1, how="all")

    if numeric_only.shape[0] <= 10 or numeric_only.shape[1] == 0:
        return 0.0

    filled = numeric_only.fillna(numeric_only.mean())
    if filled.shape[1] > 50:
        filled = filled.iloc[:, :50]

    iso = IsolationForest(n_estimators=100, contamination="auto", random_state=42)
    preds = iso.fit_predict(filled)

    return (preds == -1).sum() / filled.shape[0]


def compute_column_stats(df: pd.DataFrame, numeric_df: pd.DataFrame) -> tuple[dict, dict]:
    rows = len(df)

    uniqueness = {
        col: df[col].nunique(dropna=False) / rows if rows else 0.0
        for col in df.columns
    }

    numeric_stats = {}
    for col in df.columns:
        col_num = numeric_df[col].dropna()
        numeric_stats[col] = {} if col_num.empty else {
            "min": float(col_num.min()),
            "max": float(col_num.max()),
            "mean": float(col_num.mean()),
        }

    return uniqueness, numeric_stats


def _compute_string_length_metrics(series: pd.Series) -> Dict[str, float]:
    """
    Compute basic length statistics for a text-like series.
    Returns an empty dict if there are no non-null string values.
    """
    s = series.dropna().astype(str)
    if s.empty:
        return {}

    lengths = s.str.len()
    if lengths.empty:
        return {}

    return {
        "min": float(lengths.min()),
        "max": float(lengths.max()),
        "avg": float(lengths.mean()),
    }


def infer_column_rule(col_name: str) -> str | None:
    """
    Infers the type rule for a column based on its name.
    Supports more variations for email, name, and id columns.
    """
    name = col_name.lower()

    # ---- email variations ----
    email_keywords = ["email", "e-mail", "mail_address", "email_address"]
    if any(k in name for k in email_keywords):
        return "email"

    # ---- name variations ----
    name_keywords = ["name", "full_name", "first_name", "last_name", "username", "user_name"]
    if any(k in name for k in name_keywords):
        return "name"

    # ---- id variations ----
    id_keywords = ["id", "_id", "user_id", "employee_id", "uuid", "identifier"]
    if any(k in name for k in id_keywords):
        return "id"

    return None


def _regex_confidence(series: pd.Series, pattern: re.Pattern) -> float:
    """
    Compute the fraction (0–1) of non-null, non-empty string values
    that fully match the given compiled regex pattern.
    """
    s = series.dropna().astype(str).str.strip()
    s = s[s != ""]
    total = len(s)
    if total == 0:
        return 0.0

    matches = s.str.fullmatch(pattern).fillna(False)
    return float(matches.sum() / total)


def _detect_string_formats(series: pd.Series) -> Dict[str, float]:
    """
    Detect common string formats and return confidence ratios.
    """
    email_conf = _regex_confidence(series, _EMAIL_PATTERN)
    date_conf = _regex_confidence(series, _DATE_PATTERN)
    identifier_conf = _regex_confidence(series, _IDENTIFIER_PATTERN)

    return {
        "email": email_conf,
        "date": date_conf,
        "identifier": identifier_conf,
    }


def _numeric_distribution_metrics(series: pd.Series, bins: int = 10, sample_size: int = 100000) -> Dict[str, Any]:
    """
    Compute histogram, quantiles, skewness, kurtosis, zero-inflation, and detect constant/near-constant columns.
    Uses sampling for very large series.
    """
    s = series.dropna()
    if s.empty:
        return {}

    # Sample if very large
    if len(s) > sample_size:
        s = s.sample(sample_size, random_state=42)

    metrics: Dict[str, Any] = {}

    # histogram counts
    counts, bin_edges = np.histogram(s, bins=bins)
    metrics["histogram"] = {
        "bin_edges": bin_edges.tolist(),
        "counts": counts.tolist()
    }

    # quantiles
    quantiles = s.quantile([0.25, 0.5, 0.75, 0.95])
    metrics["quantiles"] = {str(q): float(v) for q, v in quantiles.items()}

    # skewness & kurtosis
    metrics["skewness"] = float(s.skew())
    metrics["kurtosis"] = float(s.kurt())

    # zero inflation
    metrics["zero_ratio"] = float((s == 0).sum() / len(s))

    # constant or near-constant detection
    metrics["is_constant"] = bool(s.nunique() <= 1)
    metrics["is_near_constant"] = bool(s.nunique() / len(s) < 0.01)  # <1% unique

    return metrics


def _infer_column_type_probabilities(series: pd.Series) -> tuple[str, dict[str, int]]:
    counts = {"int": 0, "float": 0, "string": 0, "datetime": 0, "bool": 0, "other": 0}

    for val in series.dropna():
        _count_value_type(val, counts)

    dominant_type = max(counts, key=counts.get)
    return dominant_type, counts


def _count_value_type(val, counts: dict[str, int]) -> None:
    if isinstance(val, bool):
        counts["bool"] += 1
        return

    if isinstance(val, int):
        counts["int"] += 1
        return

    if isinstance(val, float):
        counts["int" if val.is_integer() else "float"] += 1
        return

    if isinstance(val, str):
        _count_string_type(val, counts)
        return

    counts["other"] += 1


def _count_string_type(val: str, counts: dict[str, int]) -> None:
    val = val.strip()
    if not val:
        return

    if _can_parse(int, val):
        counts["int"] += 1
    elif _can_parse(float, val):
        counts["float"] += 1
    elif _can_parse(pd.to_datetime, val):
        counts["datetime"] += 1
    else:
        counts["string"] += 1


def _can_parse(parser, value: str) -> bool:
    try:
        parser(value)
        return True
    except Exception:
        return False

def _add_type_check(series: pd.Series, result: dict[str, Any]) -> None:
    dominant_type, _ = _infer_column_type_probabilities(series)
    result["type_check"] = dominant_type


def _update_type_consistency(
    col: str, series: pd.Series, type_consistency: dict[str, Any]
) -> None:
    rule = infer_column_rule(col)
    if not rule or col not in type_consistency:
        return

    valid, invalid = validate_series(series, rule)
    type_consistency[col]["valid"] = valid
    type_consistency[col]["invalid"] = invalid


def _infer_analysis_type(series: pd.Series, numeric_series: pd.Series) -> str:
    if pd.api.types.is_numeric_dtype(numeric_series):
        return "numeric"
    if pd.api.types.is_datetime64_any_dtype(series):
        return "datetime"
    return "categorical"


def _add_missing_metrics(series: pd.Series, rows: int, result: dict[str, Any]) -> None:
    missing = int(series.isna().sum())
    result["missing_pct"] = (missing / rows) * 100 if rows else 0.0


def _add_cardinality_metrics(series: pd.Series, rows: int, result: dict[str, Any]) -> None:
    distinct = int(series.nunique(dropna=True))
    result["cardinality"] = distinct
    result["duplicates"] = max(0, rows - distinct)


def _add_numeric_metrics(
    numeric_series: pd.Series, inferred_type: str, result: dict[str, Any]
) -> None:
    if inferred_type != "numeric":
        return

    num = numeric_series.dropna()
    if num.empty:
        return

    result.update({
        "min": float(num.min()),
        "max": float(num.max()),
        "mean": float(num.mean()),
        "count": int(num.count()),
        "median": float(num.median()),
        "std": float(num.std(ddof=0)),
        "distribution": _numeric_distribution_metrics(num),
    })


def _add_string_metrics(
    series: pd.Series, inferred_type: str, result: dict[str, Any]
) -> None:
    if inferred_type != "categorical":
        return

    metrics = _compute_string_length_metrics(series)
    if metrics:
        result["string_length"] = metrics

def analyze_columns(
    df: pd.DataFrame,
    numeric_df: pd.DataFrame,
    type_consistency: dict[str, Any],
) -> dict[str, dict[str, Any]]:
    rows = len(df)
    results: dict[str, dict[str, Any]] = {}

    for col in df.columns:
        s = df[col]
        col_result: dict[str, Any] = {}

        _add_type_check(s, col_result)
        _update_type_consistency(col, s, type_consistency)

        inferred_type = _infer_analysis_type(s, numeric_df[col])
        col_result["inferred_type"] = inferred_type

        _add_missing_metrics(s, rows, col_result)
        _add_cardinality_metrics(s, rows, col_result)
        _add_numeric_metrics(numeric_df[col], inferred_type, col_result)
        _add_string_metrics(s, inferred_type, col_result)

        results[col] = col_result

    return results

def analyze_csv(content: bytes) -> Dict[str, Any]:
    df = load_dataframe(content)

    if df.empty:
        return {
            "cleaned_data": {"rows": 0, "columns": 0},
            "quality_report": {},
        }

    rows, cols = df.shape

    missing_rate, duplicate_rate, duplicate_count = compute_basic_quality(df)

    # Identify columns that should NOT be numeric
    text_columns = [col for col in df.columns if infer_column_rule(col) in ("name", "email", "id")]

    # Convert only non-text columns
    numeric_df = df.copy()
    for col in df.columns:
        if col not in text_columns:
            numeric_df[col] = pd.to_numeric(df[col], errors="coerce")
        else:
            numeric_df[col] = pd.NA  # prevent text columns from being treated as numeric

    type_consistency = compute_type_consistency(df)
    column_analysis = analyze_columns(df, numeric_df, type_consistency)
    outlier_rate = compute_outlier_rate(numeric_df)
    uniqueness, numeric_stats = compute_column_stats(df, numeric_df)
    penalty = (missing_rate + duplicate_rate + outlier_rate) / 3.0
    quality_score = max(0.0, (1.0 - penalty) * 100)

    return {
        "cleaned_data": {"rows": rows, "columns": cols},
        "quality_report": {
            "missing_rate": round(missing_rate, 4),
            "duplicate_rate": round(duplicate_rate, 4),
            "outlier_rate": round(outlier_rate, 4),
            "duplicate_count": duplicate_count,
            "quality_score": round(quality_score, 4),
            "uniqueness": {k: round(v, 4) for k, v in uniqueness.items()},
            "type_consistency": type_consistency,
            "numeric_stats": numeric_stats,
            "column_analysis": column_analysis,
        },
    }


def analyze_csv_streaming(path: str, chunksize: int = 100_000) -> dict[str, Any]:
    state = _init_streaming_state()

    _first_pass_stream(path, chunksize, state)

    if state["total_rows"] == 0:
        return _empty_csv_result()

    numeric_stats = _finalize_numeric_stats(state)
    column_analysis = _build_column_analysis(state)
    type_consistency = _build_type_consistency(state)

    outlier_rate = _second_pass_outliers(
        path, chunksize, state["columns"], numeric_stats, state["total_rows"]
    )

    return _build_final_report(
        state, numeric_stats, column_analysis, type_consistency, outlier_rate
    )

def _init_streaming_state() -> dict[str, Any]:
    return {
        "columns": [],
        "total_rows": 0,
        "total_cells": 0,
        "total_missing_cells": 0,
        "per_col_missing": {},
        "per_col_non_missing": {},
        "rule_map": {},
        "valid_counts": {},
        "invalid_counts": {},
        "numeric_acc": {},
        "per_col_unique": {},
        "row_hashes": set(),
    }

def _first_pass_stream(path: str, chunksize: int, state: dict[str, Any]) -> None:
    first_chunk = True

    for chunk in pd.read_csv(path, chunksize=chunksize):
        if first_chunk:
            _initialize_columns(chunk, state)
            if not state["columns"]:
                return
            first_chunk = False

        _process_chunk(chunk, state)

def _initialize_columns(chunk: pd.DataFrame, state: dict[str, Any]) -> None:
    columns = list(chunk.columns)
    state["columns"] = columns

    for col in columns:
        state["per_col_missing"][col] = 0
        state["per_col_non_missing"][col] = 0
        rule = infer_column_rule(col)
        state["rule_map"][col] = rule
        state["valid_counts"][col] = 0
        state["invalid_counts"][col] = 0
        state["numeric_acc"][col] = {
            "count": 0, "sum": 0.0, "sumsq": 0.0, "min": None, "max": None
        }
        state["per_col_unique"][col] = set()

def _process_chunk(chunk: pd.DataFrame, state: dict[str, Any]) -> None:
    rows = len(chunk)
    cols = state["columns"]

    state["total_rows"] += rows
    state["total_cells"] += rows * len(cols)
    state["total_missing_cells"] += int(chunk.isna().sum().sum())

    for col in cols:
        _process_column_chunk(chunk[col], col, rows, state)

    _track_row_duplicates(chunk, cols, state)

def _process_column_chunk(
    series: pd.Series, col: str, rows: int, state: dict[str, Any]
) -> None:
    missing = int(series.isna().sum())
    state["per_col_missing"][col] += missing
    state["per_col_non_missing"][col] += rows - missing

    rule = state["rule_map"][col]
    if rule:
        v, inv = validate_series(series, rule)
        state["valid_counts"][col] += v
        state["invalid_counts"][col] += inv

    _update_numeric_acc(series, state["numeric_acc"][col])
    _update_unique_values(series, state["per_col_unique"][col])

def _update_numeric_acc(series: pd.Series, acc: dict[str, Any]) -> None:
    nums = pd.to_numeric(series, errors="coerce").dropna()
    if nums.empty:
        return

    acc["count"] += len(nums)
    acc["sum"] += float(nums.sum())
    acc["sumsq"] += float((nums * nums).sum())

    mn, mx = float(nums.min()), float(nums.max())
    acc["min"] = mn if acc["min"] is None else min(acc["min"], mn)
    acc["max"] = mx if acc["max"] is None else max(acc["max"], mx)

def _update_unique_values(series: pd.Series, store: set[str]) -> None:
    for v in series.values:
        store.add("__NA__" if pd.isna(v) else str(v))


def _track_row_duplicates(chunk: pd.DataFrame, columns: list[str], state: dict[str, Any]) -> None:
    for _, row in chunk.iterrows():
        state["row_hashes"].add(
            tuple("__NA__" if pd.isna(row[c]) else str(row[c]) for c in columns)
        )

def _finalize_numeric_stats(state: dict[str, Any]) -> dict[str, dict[str, Any]]:
    stats = {}
    for col, acc in state["numeric_acc"].items():
        if acc["count"] == 0:
            stats[col] = {}
            continue

        count = acc["count"]
        mean = acc["sum"] / count
        var = max(0.0, (acc["sumsq"] / count) - mean**2)

        stats[col] = {
            "min": acc["min"],
            "max": acc["max"],
            "mean": mean,
            "std": var ** 0.5,
        }
    return stats


def _second_pass_outliers(
    path: str,
    chunksize: int,
    columns: list[str],
    numeric_stats: dict[str, dict[str, Any]],
    total_rows: int,
) -> float:
    numeric_cols = [c for c in columns if numeric_stats.get(c)]
    outliers = 0

    for chunk in pd.read_csv(path, chunksize=chunksize):
        for _, row in chunk.iterrows():
            if _row_is_outlier(row, numeric_cols, numeric_stats):
                outliers += 1

    return outliers / total_rows if total_rows else 0.0


def _build_final_report(
    state: dict[str, Any],
    numeric_stats: dict[str, Any],
    column_analysis: dict[str, Any],
    type_consistency: dict[str, Any],
    outlier_rate: float,
) -> dict[str, Any]:
    rows = state["total_rows"]
    missing_rate = state["total_missing_cells"] / state["total_cells"]
    duplicate_count = rows - len(state["row_hashes"])
    duplicate_rate = duplicate_count / rows

    penalty = (missing_rate + duplicate_rate + outlier_rate) / 3
    quality_score = max(0.0, (1.0 - penalty) * 100)

    return {
        "cleaned_data": {"rows": rows, "columns": len(state["columns"])},
        "quality_report": {
            "missing_rate": round(missing_rate, 4),
            "duplicate_rate": round(duplicate_rate, 4),
            "outlier_rate": round(outlier_rate, 4),
            "duplicate_count": duplicate_count,
            "quality_score": round(quality_score, 4),
            "numeric_stats": numeric_stats,
            "column_analysis": column_analysis,
            "type_consistency": type_consistency,
        },
    }

def to_native(obj: Any) -> Any:
    """Recursively convert numpy types to native Python types.
    Also replaces NaN/Infinity with None for JSON safety."""
    if isinstance(obj, dict):
        return {k: to_native(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [to_native(v) for v in obj]
    if isinstance(obj, (np.integer, np.int64)):
        return int(obj)
    if isinstance(obj, (np.floating, np.float64)):
        val = float(obj)
        if math.isnan(val) or math.isinf(val):
            return None
        return val
    if isinstance(obj, np.bool_):
        return bool(obj)
    if isinstance(obj, float):
        if math.isnan(obj) or math.isinf(obj):
            return None
    return obj



# ---- Row-level outlier detection for raw data ----

def _to_float(value: Any) -> float | None:
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str) and value.strip() != "":
        try:
            return float(value)
        except ValueError:
            return None
    return None


def compute_row_outliers(
    header: list[str],
    rows: list[list[Any]],
) -> list[dict[str, Any]]:
    if not header or not rows:
        return [_no_outlier_row(r) for r in rows]

    col_count = len(header)
    numeric_values = _collect_numeric_values(rows, col_count)
    numeric_cols = [j for j in range(col_count) if numeric_values[j]]

    if not numeric_cols:
        return [_no_outlier_row(r) for r in rows]

    is_non_negative = _detect_non_negative_columns(numeric_values, numeric_cols)
    means, stds = _compute_mean_std(numeric_values, numeric_cols, col_count)

    return [
        _enrich_row(row, numeric_cols, is_non_negative, means, stds)
        for row in rows
    ]

def _no_outlier_row(row: list[Any]) -> dict[str, Any]:
    return {
        "values": row,
        "is_outlier": False,
        "outlier_score": 0.0,
        "outlier_severity": "NONE",
    }

def _collect_numeric_values(
    rows: list[list[Any]], col_count: int
) -> list[list[float]]:
    values = [[] for _ in range(col_count)]
    for row in rows:
        for j in range(min(len(row), col_count)):
            num = _to_float(row[j])
            if num is not None:
                values[j].append(num)
    return values


def _detect_non_negative_columns(
    numeric_values: list[list[float]],
    numeric_cols: list[int],
) -> list[bool]:
    result = [False] * len(numeric_values)
    for j in numeric_cols:
        vals = numeric_values[j]
        if vals and sum(v >= 0 for v in vals) / len(vals) >= 0.9:
            result[j] = True
    return result

def _compute_mean_std(
    numeric_values: list[list[float]],
    numeric_cols: list[int],
    col_count: int,
) -> tuple[list[float], list[float]]:
    means = [math.nan] * col_count
    stds = [math.nan] * col_count

    for j in numeric_cols:
        vals = numeric_values[j]
        mean = sum(vals) / len(vals)
        var = sum((v - mean) ** 2 for v in vals) / len(vals)
        means[j] = mean
        stds[j] = math.sqrt(var)

    return means, stds
def _enrich_row(
    row: list[Any],
    numeric_cols: list[int],
    is_non_negative: list[bool],
    means: list[float],
    stds: list[float],
) -> dict[str, Any]:
    max_z = 0.0
    invalid_negative = False

    for j in numeric_cols:
        if j >= len(row):
            continue

        num = _to_float(row[j])
        if num is None:
            continue

        if is_non_negative[j] and num < 0:
            invalid_negative = True

        z = _z_score(num, means[j], stds[j])
        if z > max_z:
            max_z = z

    return _build_outlier_result(row, invalid_negative, max_z)

def _z_score(x: float, mean: float, std: float) -> float:
    if std and std > 0:
        z = abs((x - mean) / std)
        return z if math.isfinite(z) else 0.0
    return 0.0

def _build_outlier_result(
    row: list[Any],
    invalid_negative: bool,
    max_z: float,
) -> dict[str, Any]:
    if invalid_negative:
        return {
            "values": row,
            "is_outlier": True,
            "outlier_score": 999.0,
            "outlier_severity": "HIGH",
        }

    if max_z <= 3:
        return _no_outlier_row(row)

    if max_z <= 4:
        severity = "LOW"
    elif max_z <= 5.5:
        severity = "MEDIUM"
    else:
        severity = "HIGH"

    return {
        "values": row,
        "is_outlier": True,
        "outlier_score": float(max_z),
        "outlier_severity": severity,
    }
