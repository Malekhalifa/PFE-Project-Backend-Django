"""
py_profiler.py — Deterministic Python-based statistical profiler step.

Replaces the LLM profiler agent with a concurrent SQL-based implementation
that generates and executes the same statistical queries directly. No LLM
tokens consumed, no AFC overhead, no MALFORMED_FUNCTION_CALL risk.

Typical speedup vs the LLM profiler: 10-15× (156s → ~10s).
"""

from __future__ import annotations

import logging
import math
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any

import fnmatch

import psycopg2
from psycopg2.extras import RealDictCursor

from agno.workflow.types import StepInput, StepOutput

from .schemas import Finding, ProfilerColumnStat, ProfilerReport, ProfilerTableStat, ValidationReport

logger = logging.getLogger(__name__)

_SAFE_IDENTIFIER = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]*$")

_NUMERIC_TYPES: frozenset[str] = frozenset({
    "integer", "bigint", "smallint", "serial", "bigserial", "smallserial",
    "int", "int2", "int4", "int8",
    "numeric", "decimal",
    "real", "double precision", "float", "float4", "float8",
    "money",
})

_TEMPORAL_TYPES: frozenset[str] = frozenset({
    "date",
    "timestamp", "timestamp without time zone", "timestamp with time zone",
    "timestamptz",
    "time", "time without time zone", "time with time zone", "timetz",
    "interval",
})

_MAX_WORKERS = 8
_STATEMENT_TIMEOUT_MS = 30_000


def _classify(data_type: str) -> str:
    """Return 'numeric', 'temporal', or 'categorical' for a PostgreSQL data type string."""
    dt = data_type.lower().split("(")[0].strip()
    if dt in _NUMERIC_TYPES:
        return "numeric"
    if dt in _TEMPORAL_TYPES:
        return "temporal"
    return "categorical"


def _qi(name: str) -> str:
    """Double-quote a PostgreSQL identifier."""
    return '"' + name.replace('"', '""') + '"'


def _alias(name: str, seen: set[str]) -> str:
    """Return a unique SQL alias prefix for a column name."""
    base = re.sub(r"[^a-z0-9]", "_", name.lower())
    if base not in seen:
        seen.add(base)
        return base
    i = 1
    while f"{base}_{i}" in seen:
        i += 1
    alias = f"{base}_{i}"
    seen.add(alias)
    return alias


def _build_stats_sql(table_name: str, col_info: list[tuple[str, str, str]]) -> str:
    """
    Build a single SELECT that collects null_rate, cardinality, and numeric/temporal
    extended stats for every column in one table scan.

    col_info: list of (col_name, alias_prefix, kind)
    """
    qt = _qi(table_name)
    parts: list[str] = ["COUNT(*) AS _total_rows"]

    for col_name, al, kind in col_info:
        qc = _qi(col_name)
        parts.append(
            f'COUNT(*) FILTER (WHERE {qc} IS NULL)::float / NULLIF(COUNT(*), 0) AS "{al}__nr"'
        )
        parts.append(f'COUNT(DISTINCT {qc}) AS "{al}__card"')

        if kind == "numeric":
            parts += [
                f'MIN({qc}::numeric)::text AS "{al}__min"',
                f'MAX({qc}::numeric)::text AS "{al}__max"',
                f'AVG({qc}::numeric)       AS "{al}__mean"',
                f'STDDEV({qc}::numeric)    AS "{al}__stddev"',
                f'PERCENTILE_CONT(0.01) WITHIN GROUP (ORDER BY {qc}::numeric) AS "{al}__p01"',
                f'PERCENTILE_CONT(0.50) WITHIN GROUP (ORDER BY {qc}::numeric) AS "{al}__p50"',
                f'PERCENTILE_CONT(0.99) WITHIN GROUP (ORDER BY {qc}::numeric) AS "{al}__p99"',
            ]
        elif kind == "temporal":
            parts += [
                f'MIN({qc})::text AS "{al}__min"',
                f'MAX({qc})::text AS "{al}__max"',
            ]

    return "SELECT\n    " + ",\n    ".join(parts) + f"\nFROM {qt}"


def _build_outlier_sql(
    table_name: str,
    numeric_bounds: list[tuple[str, str, float, float]],
) -> str | None:
    """
    One SELECT that counts outliers for each numeric column using bounds
    computed from p01/p99 retrieved in the stats query.

    numeric_bounds: (col_name, alias_prefix, lower_bound, upper_bound)
    Returns None when the list is empty.
    """
    if not numeric_bounds:
        return None

    qt = _qi(table_name)
    parts: list[str] = []
    for col_name, al, lower, upper in numeric_bounds:
        qc = _qi(col_name)
        # Embed bounds as float literals — both are finite (checked by caller)
        parts.append(
            f'(COUNT(*) FILTER (WHERE {qc}::numeric < {lower!r} OR {qc}::numeric > {upper!r})) > 0'
            f' AS "{al}__outlier"'
        )

    return "SELECT\n    " + ",\n    ".join(parts) + f"\nFROM {qt}"


def _profile_table(
    table_name: str,
    column_details: list[dict[str, str]],
    connection_string: str,
) -> ProfilerTableStat:
    """
    Profile one table: two queries (stats + outlier check), one reused connection.
    Returns a populated ProfilerTableStat; falls back to row_count=0 on any failure.
    """
    # Build column metadata
    seen_aliases: set[str] = set()
    col_info: list[tuple[str, str, str]] = []
    for col in column_details:
        col_name = col.get("column_name", "").strip()
        if not col_name:
            continue
        kind = _classify(col.get("data_type", ""))
        col_info.append((col_name, _alias(col_name, seen_aliases), kind))

    if not col_info:
        return ProfilerTableStat(table_name=table_name, row_count=0, column_stats=[])

    stats_sql = _build_stats_sql(table_name, col_info)
    base_row: dict[str, Any] = {}
    outlier_row: dict[str, Any] = {}

    conn = None
    try:
        conn = psycopg2.connect(connection_string)
        conn.autocommit = True

        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(f"SET statement_timeout = {_STATEMENT_TIMEOUT_MS}")

            # Query 1: all stats in one scan
            try:
                cur.execute(stats_sql)
                row = cur.fetchone()
                base_row = dict(row) if row else {}
            except Exception as exc:
                logger.warning("py_profiler: stats query failed for '%s': %s", table_name, exc)

            total_rows = int(base_row.get("_total_rows") or 0)

            # Query 2: outlier check — only when the table has rows
            if total_rows > 0:
                numeric_bounds: list[tuple[str, str, float, float]] = []
                for col_name, al, kind in col_info:
                    if kind != "numeric":
                        continue
                    p01_v = base_row.get(f"{al}__p01")
                    p99_v = base_row.get(f"{al}__p99")
                    if p01_v is None or p99_v is None:
                        continue
                    try:
                        p01_f, p99_f = float(p01_v), float(p99_v)
                    except (TypeError, ValueError):
                        continue
                    iqr = p99_f - p01_f
                    lower = p01_f - 3.0 * iqr
                    upper = p99_f + 3.0 * iqr
                    if math.isfinite(lower) and math.isfinite(upper):
                        numeric_bounds.append((col_name, al, lower, upper))

                outlier_sql = _build_outlier_sql(table_name, numeric_bounds)
                if outlier_sql:
                    try:
                        cur.execute(outlier_sql)
                        orow = cur.fetchone()
                        outlier_row = dict(orow) if orow else {}
                    except Exception as exc:
                        logger.warning(
                            "py_profiler: outlier query failed for '%s': %s", table_name, exc
                        )

    except Exception as exc:
        logger.error("py_profiler: connection failed for table '%s': %s", table_name, exc)
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass

    total_rows = int(base_row.get("_total_rows") or 0)
    column_stats: list[ProfilerColumnStat] = []

    for col_name, al, kind in col_info:
        null_rate_raw = base_row.get(f"{al}__nr")
        cardinality_raw = base_row.get(f"{al}__card")
        null_rate = max(0.0, min(1.0, float(null_rate_raw or 0.0)))
        cardinality = int(cardinality_raw or 0)
        outlier_flag = bool(outlier_row.get(f"{al}__outlier", False))

        stat = ProfilerColumnStat(
            column_name=col_name,
            null_rate=null_rate,
            cardinality=cardinality,
            total_rows=total_rows,
            outlier_flag=outlier_flag,
        )

        if kind == "numeric":
            v = base_row.get(f"{al}__min")
            if v is not None:
                stat.min_value = str(v)
            v = base_row.get(f"{al}__max")
            if v is not None:
                stat.max_value = str(v)
            for attr, key in (
                ("mean",   f"{al}__mean"),
                ("stddev", f"{al}__stddev"),
                ("p01",    f"{al}__p01"),
                ("p50",    f"{al}__p50"),
                ("p99",    f"{al}__p99"),
            ):
                v = base_row.get(key)
                if v is not None:
                    try:
                        setattr(stat, attr, float(v))
                    except (TypeError, ValueError):
                        pass
        elif kind == "temporal":
            v = base_row.get(f"{al}__min")
            if v is not None:
                stat.min_value = str(v)
            v = base_row.get(f"{al}__max")
            if v is not None:
                stat.max_value = str(v)

        column_stats.append(stat)

    return ProfilerTableStat(
        table_name=table_name,
        row_count=total_rows,
        column_stats=column_stats,
    )


def _build_schema_from_db(connection_string: str) -> dict[str, Any]:
    """
    Minimal schema dict {table_name: {column_details}} built directly from
    information_schema when session_state["schema"] is unavailable.

    Fallback for when ensure_schema_primed's state mutations don't propagate
    to the Step executor's workflow_session.session_data.
    """
    try:
        from .rules import domain_rules as load_domain_rules
        rules = load_domain_rules()
        excluded_patterns: list[str] = list(rules.get("excluded_tables", []))
    except Exception:
        excluded_patterns = []

    schema: dict[str, Any] = {}
    conn = None
    try:
        conn = psycopg2.connect(connection_string)
        conn.autocommit = True
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                "SELECT table_name FROM information_schema.tables"
                " WHERE table_schema = 'public' AND table_type = 'BASE TABLE'"
                " ORDER BY table_name"
            )
            all_tables = [row["table_name"] for row in cur.fetchall()]
            tables = [
                t for t in all_tables
                if _SAFE_IDENTIFIER.match(t)
                and not any(fnmatch.fnmatch(t, pat) for pat in excluded_patterns)
            ]
            if not tables:
                return schema

            cur.execute(
                "SELECT table_name, column_name, data_type, is_nullable"
                " FROM information_schema.columns"
                " WHERE table_schema = 'public' AND table_name = ANY(%s)"
                " ORDER BY table_name, ordinal_position",
                (tables,),
            )
            for row in cur.fetchall():
                tbl = row["table_name"]
                if tbl not in schema:
                    schema[tbl] = {"column_details": []}
                schema[tbl]["column_details"].append({
                    "column_name": row["column_name"],
                    "data_type":   row["data_type"],
                    "is_nullable": row["is_nullable"],
                })
        logger.info("py_profiler: _build_schema_from_db loaded %d tables", len(schema))
    except Exception as exc:
        logger.error("py_profiler: _build_schema_from_db failed: %s", exc)
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass
    return schema


_TS_SUFFIXES = ("updated_at", "modified_at", "last_used", "last_time", "last_login", "last_seen")


def _compute_statistical_skip_reason(
    schema: dict[str, Any],
    profiler_summary: dict[str, Any],
    domain_rules: dict[str, Any],
) -> str:
    """
    Return a non-empty skip reason when the statistical LLM pass would produce
    zero tool calls — i.e. no unique-pattern columns and no low-cardinality
    columns exist in this database.

    The statistical agent reads {statistical_skip_reason} and returns an empty
    ValidationReport immediately without calling any tool, saving one full
    Gemini API round-trip (~30-100 tool calls).
    """
    unique_patterns: list[str] = domain_rules.get("unique_column_patterns") or []
    min_rows: int = int(domain_rules.get("min_rows_for_cardinality") or 100)

    has_unique_col = False
    for table_info in schema.values():
        for col in table_info.get("column_details", []):
            col_name = (col.get("column_name") or "").lower()
            for pattern in unique_patterns:
                if pattern.startswith("*"):
                    if col_name.endswith(pattern[1:].lower()):
                        has_unique_col = True
                        break
                elif col_name == pattern.lower():
                    has_unique_col = True
                    break
            if has_unique_col:
                break
        if has_unique_col:
            break

    has_low_cardinality = False
    for table_name, table_stats in profiler_summary.items():
        row_count = table_stats.get("row_count") or 0
        if row_count < min_rows:
            continue
        for col_stats in table_stats.get("columns", {}).values():
            card = col_stats.get("cardinality") or 0
            if 2 <= card <= 50:
                has_low_cardinality = True
                break
        if has_low_cardinality:
            break

    if not has_unique_col and not has_low_cardinality:
        return (
            "No columns matching unique_column_patterns exist in this schema and "
            "no low-cardinality columns (cardinality 2–50) were detected by the profiler. "
            "The py_profiler auto-findings already cover all statistical issues for this run. "
            "Return an empty ValidationReport immediately — do not make any tool calls."
        )
    return ""


def _derive_findings(
    report: ProfilerReport,
    domain_rules: dict[str, Any],
) -> list[dict[str, Any]]:
    """
    Convert profiler column stats into deterministic statistical findings.

    Eliminates LLM non-determinism for obvious null-rate violations by applying
    tiered thresholds from domain_rules.yaml. Results are stored in
    session_state["profiler_auto_findings"] and merged into the statistical
    report by the orchestrator before reduction.
    """
    try:
        nt = domain_rules.get("null_thresholds") or {}
        thr_fk   = float(nt.get("fk_columns",        0.10))
        thr_ts   = float(nt.get("timestamp_columns",  0.80))
        thr_dflt = float(nt.get("default",            0.40))
    except Exception:
        thr_fk, thr_ts, thr_dflt = 0.10, 0.80, 0.40

    findings: list[dict[str, Any]] = []
    for tbl_stat in report.tables:
        tbl = tbl_stat.table_name
        if tbl_stat.row_count == 0:
            continue
        for col_stat in tbl_stat.column_stats:
            col = col_stat.column_name
            null_rate = col_stat.null_rate
            if null_rate == 0.0:
                continue

            if col.endswith("_id"):
                threshold = thr_fk
            elif any(col.endswith(s) or s in col for s in _TS_SUFFIXES):
                threshold = thr_ts
            else:
                threshold = thr_dflt

            if null_rate > threshold:
                severity = "high" if null_rate >= 0.80 else "medium"
                findings.append({
                    "validator": "statistical",
                    "entity": f"{tbl}.{col}",
                    "rule": "null_ratio_high",
                    "severity": severity,
                    "confidence": 0.95,
                    "evidence": {
                        "null_ratio": round(null_rate, 4),
                        "total_rows": col_stat.total_rows,
                        "threshold_used": threshold,
                    },
                    "suggested_action": (
                        f"Investigate why '{tbl}.{col}' has {null_rate:.0%} null values. "
                        "Consider enforcing NOT NULL if this column is required."
                    ),
                })

    return findings


def _derive_schema_findings(
    schema: dict[str, Any],
    domain_rules: dict[str, Any],
) -> list[dict[str, Any]]:
    """
    Convert the schema snapshot into deterministic structural findings.

    Three rules, all 100% deterministic from PostgreSQL metadata:
    1. missing_pk              — table has no primary key
    2. nullable_fk_style_column — _id column is nullable (nullable_fk_by_design excluded)
    3. missing_index_fk_column  — actual FK column has no index (orphan queries will full-scan)

    Results stored in session_state["schema_auto_findings"] and merged into the
    schema report after the schema agent runs, eliminating LLM non-determinism on
    pure-metadata checks.
    """
    nullable_by_design: dict[str, list[str]] = {}
    try:
        nbd = domain_rules.get("nullable_fk_by_design") or {}
        if isinstance(nbd, dict):
            nullable_by_design = {k: list(v) for k, v in nbd.items()}
    except Exception:
        pass

    findings: list[dict[str, Any]] = []

    for table_name, entry in schema.items():
        # Rule 1: missing primary key
        if not entry.get("has_primary_key") and not entry.get("primary_key_columns"):
            findings.append({
                "validator": "schema",
                "entity": table_name,
                "rule": "missing_pk",
                "severity": "high",
                "confidence": 1.0,
                "evidence": {"has_primary_key": False},
                "suggested_action": f"Add a PRIMARY KEY to '{table_name}'.",
            })

        # Rule 2: nullable ACTUAL FK columns (restricted to foreign_keys list — not style inference).
        # Only flag columns that have an explicit FK constraint AND are nullable.
        # "Style" columns (named like _id but no FK constraint) are left to the LLM schema agent.
        fk_col_names: set[str] = {fk.get("column", "") for fk in entry.get("foreign_keys", [])}
        nbd_cols: set[str] = set(nullable_by_design.get(table_name, []))
        for col in entry.get("column_details", []):
            col_name = col.get("column_name", "")
            if col_name not in fk_col_names:
                continue
            if col.get("is_nullable", "").upper() != "YES":
                continue
            if col_name in nbd_cols:
                continue
            findings.append({
                "validator": "schema",
                "entity": f"{table_name}.{col_name}",
                "rule": "nullable_fk_column",
                "severity": "medium",
                "confidence": 0.92,
                "evidence": {"is_nullable": True, "has_fk_constraint": True},
                "suggested_action": (
                    f"'{table_name}.{col_name}' has a FK constraint but is nullable. "
                    "Add NOT NULL if every row must reference a valid parent record."
                ),
            })

        # Rule 3: unindexed actual FK columns (orphan queries will full-scan without an index)
        indexed: set[str] = set(entry.get("indexed_columns", []))
        for fk in entry.get("foreign_keys", []):
            fk_col = fk.get("column", "")
            if not fk_col or fk_col in indexed:
                continue
            findings.append({
                "validator": "schema",
                "entity": f"{table_name}.{fk_col}",
                "rule": "missing_index_fk_column",
                "severity": "medium",
                "confidence": 0.95,
                "evidence": {
                    "foreign_table": fk.get("foreign_table", ""),
                    "foreign_column": fk.get("foreign_column", ""),
                },
                "suggested_action": (
                    f"Add an index on '{table_name}.{fk_col}' "
                    f"(FK → {fk.get('foreign_table', '?')}.{fk.get('foreign_column', '?')}) "
                    "to speed up JOIN and orphan-check queries."
                ),
            })

    return findings


def _prioritize_profiler_summary(
    summary: dict[str, Any],
    schema: dict[str, Any],
) -> dict[str, Any]:
    """Re-order profiler_summary so high-risk columns appear first in agent prompts.

    LLMs attend better to early context. Sorting by risk score ensures the most
    suspect columns are seen first regardless of profiling completion order.

    Score per column:
      null_rate * 3  +  is_fk * 2  +  outlier_flag * 1.5  +  low_cardinality * 1
    Tables are also sorted by their highest per-column score (descending).
    """
    def _col_score(col_stats: dict[str, Any], is_fk: bool) -> float:
        null_rate = float(col_stats.get("null_rate") or 0.0)
        outlier   = bool(col_stats.get("outlier_flag"))
        card      = int(col_stats.get("cardinality") or 0)
        rows      = int(col_stats.get("total_rows") or 0)
        low_card  = (2 <= card <= 10) and rows > 100
        return null_rate * 3.0 + int(is_fk) * 2.0 + int(outlier) * 1.5 + int(low_card) * 1.0

    prioritized: dict[str, Any] = {}
    table_max_scores: list[tuple[float, str]] = []

    for table_name, table_data in summary.items():
        fk_cols: set[str] = set()
        schema_entry = schema.get(table_name) or {}
        for fk in schema_entry.get("foreign_keys", []):
            col = fk.get("column", "")
            if col:
                fk_cols.add(col)

        cols: dict[str, Any] = table_data.get("columns") or {}
        scored: list[tuple[float, str, dict]] = [
            (_col_score(stats, col_name in fk_cols), col_name, stats)
            for col_name, stats in cols.items()
        ]
        scored.sort(key=lambda x: x[0], reverse=True)

        max_score = scored[0][0] if scored else 0.0
        table_max_scores.append((max_score, table_name))

        prioritized[table_name] = {
            "row_count": table_data.get("row_count", 0),
            "columns": {col_name: stats for _, col_name, stats in scored},
        }

    table_max_scores.sort(key=lambda x: x[0], reverse=True)
    result = {tbl: prioritized[tbl] for _, tbl in table_max_scores}

    # Log top 3 highest-risk columns for observability
    top_cols = []
    for _, tbl in table_max_scores[:5]:
        cols = prioritized[tbl].get("columns", {})
        for col_name, stats in list(cols.items())[:1]:
            score = _col_score(stats, False)
            top_cols.append(f"{tbl}.{col_name}(score={score:.1f})")
    if top_cols:
        logger.info("py_profiler: priority sort — top columns: %s", ", ".join(top_cols))

    return result


def python_profile_step(step_input: StepInput) -> StepOutput:
    """
    Pure-Python profiler step executor. Replaces Step(agent=members.profiler).

    Reads schema from session_state["schema"], profiles all tables concurrently
    with a thread pool, then writes profiler_summary, profiler_report, and
    profiler_tables into session_state — the same keys store_profiler_output
    normally writes so all downstream agents see their baseline unchanged.
    """
    ws = getattr(step_input, "workflow_session", None)
    session_state: dict[str, Any] = (getattr(ws, "session_data", None) or {}) if ws else {}

    import time as _time
    _profiler_t0 = _time.perf_counter()

    schema: dict[str, Any] = session_state.get("schema") or {}
    connection_string: str = session_state.get("connection_string") or ""

    # Mirror emit_pipeline_status — the profiler agent's pre_hook won't fire here
    job_id = session_state.get("job_id")
    if job_id:
        try:
            from api.db import update_job_status
            update_job_status(job_id, "running_profiler")
            logger.info(
                "pipeline_status: running_profiler job=%s source=py_profiler_executor",
                job_id,
            )
        except Exception:
            pass
    else:
        logger.warning(
            "pipeline_status: SKIPPED running_profiler — job_id missing from session_data "
            "(_make_profiler_executor injection failed)"
        )
        try:
            from asgiref.sync import async_to_sync
            from channels.layers import get_channel_layer
            channel_layer = get_channel_layer()
            if channel_layer is not None:
                async_to_sync(channel_layer.group_send)(
                    f"job_{job_id}", {"type": "job.status", "status": "running_profiler"}
                )
        except Exception:
            pass

    if not schema:
        if connection_string:
            logger.warning(
                "py_profiler: schema not in session_state — "
                "building directly from DB (ensure_schema_primed state not propagated)"
            )
            schema = _build_schema_from_db(connection_string)
        if not schema:
            logger.error("py_profiler: schema unavailable and DB fallback produced nothing")
            return StepOutput(content=ProfilerReport(tables=[]))

    if not connection_string:
        logger.error("py_profiler: no connection_string in session_state")
        return StepOutput(content=ProfilerReport(tables=[]))

    table_names = list(schema.keys())
    logger.info("py_profiler: profiling %d tables (%d workers)", len(table_names), _MAX_WORKERS)

    try:
        results: list[ProfilerTableStat] = []
        with ThreadPoolExecutor(max_workers=_MAX_WORKERS) as pool:
            future_to_table = {
                pool.submit(
                    _profile_table,
                    tbl,
                    schema[tbl].get("column_details", []),
                    connection_string,
                ): tbl
                for tbl in table_names
            }
            for future in as_completed(future_to_table):
                tbl = future_to_table[future]
                try:
                    results.append(future.result())
                except Exception as exc:
                    logger.error("py_profiler: uncaught error for '%s': %s", tbl, exc)
                    results.append(ProfilerTableStat(table_name=tbl, row_count=0, column_stats=[]))

        report = ProfilerReport(tables=results)

        # Mirror what store_profiler_output does for the LLM profiler
        if ws is not None:
            session_state["profiler_report"] = report.model_dump()
            raw_summary = {
                t.table_name: {
                    "row_count": t.row_count,
                    "columns": {
                        c.column_name: {
                            "null_rate":    c.null_rate,
                            "outlier_flag": c.outlier_flag,
                            "cardinality":  c.cardinality,
                            "total_rows":   c.total_rows,
                        }
                        for c in t.column_stats
                    },
                }
                for t in report.tables
            }
            session_state["profiler_summary"] = _prioritize_profiler_summary(
                raw_summary, schema
            )
            session_state["structural_profiler_summary"] = {
                t.table_name: t.row_count for t in report.tables
            }
            session_state["profiler_tables"] = [t.table_name for t in report.tables]

        covered = sum(1 for t in report.tables if t.row_count > 0 or t.column_stats)

        # Deterministic statistical findings — bypass LLM non-determinism for
        # obvious null-rate violations. Stored in session_state so the orchestrator
        # can merge them into the statistical report before reduction.
        if ws is not None:
            try:
                from .rules import domain_rules as load_domain_rules
                _dr = load_domain_rules()
            except Exception:
                _dr = {}
            auto_findings = _derive_findings(report, _dr)
            session_state["profiler_auto_findings"] = auto_findings
            if auto_findings:
                logger.info(
                    "py_profiler: derived %d deterministic statistical finding(s) from profiler stats",
                    len(auto_findings),
                )

            schema_findings = _derive_schema_findings(schema, _dr)
            session_state["schema_auto_findings"] = schema_findings
            if schema_findings:
                logger.info(
                    "py_profiler: derived %d deterministic schema finding(s) from schema snapshot",
                    len(schema_findings),
                )

            skip_reason = _compute_statistical_skip_reason(
                schema, session_state.get("profiler_summary", {}), _dr
            )
            session_state["statistical_skip_reason"] = skip_reason
            if skip_reason:
                logger.info("py_profiler: statistical LLM skip flagged — no unique-pattern or low-cardinality columns")

        if covered < len(table_names) * 0.5:
            logger.warning(
                "py_profiler: low coverage — only %d/%d tables profiled successfully; "
                "statistical findings may be incomplete.",
                covered,
                len(table_names),
            )

        elapsed_ms = round((_time.perf_counter() - _profiler_t0) * 1000)
        if ws is not None:
            timing = session_state.setdefault("_phase_timing_ms", {})
            timing["Python Profiler"] = elapsed_ms

        logger.info(
            "run_complete agent=Python Profiler ms=%d tool_calls=0 findings=0 tables=%d/%d",
            elapsed_ms,
            covered,
            len(table_names),
        )
        return StepOutput(content=report)

    except Exception as exc:
        logger.error(
            "py_profiler: uncaught exception in profiling loop — returning empty report "
            "so orchestrator recovery path can take over: %s", exc
        )
        return StepOutput(content=ProfilerReport(tables=[]))
