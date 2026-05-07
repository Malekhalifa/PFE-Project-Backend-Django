"""
hooks.py - Lifecycle hooks and runtime helpers for the QualityLab pipeline.
"""

from __future__ import annotations

import csv
import fnmatch
import hashlib
import io
import json
import logging
import os
import re
import time
from typing import Any

from agno.exceptions import CheckTrigger, InputCheckError, OutputCheckError
from agno.run import RunContext
from agno.run.agent import RunInput, RunOutput

from services.postgres_connection import assert_valid_postgres_dsn, looks_like_postgres_dsn

from .schemas import FixPlan, ProfilerReport, ValidationReport
from .tool_factories import build_postgres_tools

logger = logging.getLogger("qualitylab")

_DSN_PATTERN = re.compile(r"(postgres(?:ql)?://[^\s'\"`<>]+)", re.IGNORECASE)
_WS_RE = re.compile(r"\s+")
_SAFE_IDENTIFIER = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]*$")


def normalize_query(query: str) -> str:
    """Lowercase and collapse all whitespace to a single space."""
    return _WS_RE.sub(" ", query.lower()).strip()


def _agent_id(run_context: RunContext) -> str:
    """Return the current agent's id string, or 'global' when not inside an agent.

    Used to scope per-agent session_state keys so that parallel agents running
    concurrently (statistical / structural / relational) each maintain independent
    dedup caches.  Without scoping, schema+profiler queries poison the parallel
    agents' _query_patterns list and they receive fake 'no rows returned' results.
    """
    return getattr(getattr(run_context, "agent", None), "id", "") or "global"


def _redis_client():
    try:
        import redis as _redis
        url = os.environ.get("REDIS_URL", "redis://localhost:6379/0")
        return _redis.from_url(url, decode_responses=True, socket_connect_timeout=1)
    except Exception:
        return None

def qualitylab_tool_hook(
    run_context: RunContext,
    function_name: str,
    function_call,
    arguments: dict,
):
    """Shared tool hook: query normalization, dedup, loop detection, timing telemetry.

    All dedup state (_tool_calls_seen, _query_patterns, _empty_results_streak) is
    scoped per-agent using the agent's id.  This prevents parallel validators
    (statistical / structural / relational) from sharing cache state with the schema
    and profiler agents that ran before them.
    """
    if run_context.session_state is None:
        return function_call(**arguments)
    aid = _agent_id(run_context)
    job_id = (run_context.session_state or {}).get("job_id") or "nojob"
    seen: dict[str, Any] = run_context.session_state.setdefault(f"_tool_calls_seen_{aid}", {})

    # ── Query-pattern deduplication (run_query only) ─────────────────────
    if function_name in ("run_query", "_run_query"):
        raw_sql = arguments.get("query", "")
        norm = normalize_query(raw_sql)
        # Use a list (not a set) — sets are not JSON-serializable and break
        # Agno's SQLite session upsert with "Object of type set is not JSON serializable".
        # Scoped per-agent so parallel agents don't share query-pattern history.
        patterns: list[str] = run_context.session_state.setdefault(f"_query_patterns_{aid}", [])
        if norm in patterns:
            logger.info(
                "tool_hook: duplicate query pattern skipped | agent=%s | tool=%s | norm=%.120s",
                aid,
                function_name,
                norm,
            )
            return "Query executed successfully - no rows returned."
        patterns.append(norm)

    # ── Argument-level L1 dedup ──────────────────────────────────────────
    try:
        arg_str = json.dumps(arguments, sort_keys=True, default=str)
    except Exception:
        arg_str = str(arguments)

    local_key = f"{function_name}:{arg_str}"
    # Read-only queries are database-scoped so the cache stays warm across pipeline runs
    # against the same database (e.g. repeated analysis during development). Write-path or
    # job-sensitive calls remain job-scoped to avoid cross-run contamination.
    raw_sql = arguments.get("query", "") or ""
    is_read_only = function_name not in ("run_query", "_run_query") or \
                   raw_sql.strip().upper().startswith("SELECT")
    if is_read_only:
        db_id = (run_context.session_state or {}).get("database_id") or "unknown"
        arg_hash = hashlib.md5(arg_str.encode(), usedforsecurity=False).hexdigest()
        redis_key = f"ql:schema:{db_id}:{function_name}:{arg_hash}"
    else:
        redis_key = f"ql:tool:{job_id}:{aid}:{local_key}"

    if local_key in seen:
        logger.debug("tool_hook: L1 hit for %s", function_name)
        return seen[local_key]

    # L2 — Redis hit
    r = _redis_client()
    if r is not None:
        try:
            cached = r.get(redis_key)
            if cached is not None:
                logger.debug("tool_hook: L2 Redis hit for %s", function_name)
                result = json.loads(cached)
                seen[local_key] = result
                return result
        except Exception:
            pass

    t0 = time.perf_counter()
    result = function_call(**arguments)
    elapsed_ms = (time.perf_counter() - t0) * 1000

    # ── Loop detection (per-agent streak to avoid parallel-agent cross-talk) ───
    # Tools that scan for anomalies on individual columns legitimately return empty
    # when data is clean (no FD violations, no duplicates). Counting those as
    # "loop indicators" causes false positives when an agent scans many clean columns
    # in sequence.  Non-empty results from ANY tool reset the streak as normal.
    _LOOP_EXEMPT_TOOLS = frozenset({"detect_fd", "find_duplicate_rows"})

    streak_key = f"_empty_results_streak_{aid}"
    is_empty = (
        result is None
        or result == "Query executed successfully - no rows returned."
        or (isinstance(result, str) and '"count": 0' in result)
        or (isinstance(result, str) and result.strip() == "[]")
    )
    streak: int = run_context.session_state.get(streak_key, 0)
    if not is_empty:
        run_context.session_state[streak_key] = 0
    elif function_name not in _LOOP_EXEMPT_TOOLS:
        streak += 1
        run_context.session_state[streak_key] = streak
        if streak >= 6:
            logger.warning(
                "tool_hook: LOOP DETECTED — %d consecutive empty results, halting tool execution | agent=%s | tool=%s",
                streak,
                aid,
                function_name,
            )
            raise RuntimeError(
                f"Execution halted: {streak} consecutive empty query results detected. "
                "The agent is looping. Check your analysis strategy."
            )

    # ── Enhanced logging ─────────────────────────────────────────────────
    raw_query = arguments.get("query", "")
    norm_q = normalize_query(raw_query) if raw_query else ""
    result_size = len(result) if isinstance(result, str) else len(json.dumps(result, default=str))
    truncated_flag = result_size >= 20_000

    logger.info(
        "tool=%s ms=%.1f result_bytes=%d truncated=%s | norm_query=%.120s | raw_query=%.80s",
        function_name,
        elapsed_ms,
        result_size,
        truncated_flag,
        norm_q,
        raw_query[:80],
    )

    seen[local_key] = result

    if r is not None:
        try:
            r.setex(redis_key, 3600, json.dumps(result, default=str))
        except Exception:
            pass
    return result


def _extract_input_text(run_input: Any) -> str:
    value = getattr(run_input, "input", run_input)
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    return str(value)


def _csv_rows(payload: Any) -> list[dict[str, str]]:
    text = str(payload or "").strip()
    if not text:
        return []
    reader = csv.DictReader(io.StringIO(text))
    return [
        {str(key): (value or "").strip() for key, value in row.items()}
        for row in reader
        if row
    ]


def _normalize_table_list_result(payload: Any) -> list[str]:
    rows = _csv_rows(payload)
    if rows:
        first_key = next(iter(rows[0].keys()), "")
        return [
            row.get("table_name") or row.get(first_key, "")
            for row in rows
            if row.get("table_name") or row.get(first_key, "")
        ]

    text = str(payload or "").strip()
    if not text:
        return []

    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if len(lines) > 1:
        candidate_lines = lines[1:] if "table" in lines[0].lower() else lines
        return [line for line in candidate_lines if "," not in line]

    return lines


def _normalize_describe_table_result(table_name: str, payload: Any) -> dict[str, Any]:
    rows = _csv_rows(payload)
    columns: list[str] = []
    column_details: list[dict[str, str]] = []

    for row in rows:
        column_name = (row.get("column_name") or row.get("column") or "").strip()
        if not column_name:
            continue
        columns.append(column_name)
        column_details.append(
            {
                "column_name": column_name,
                "data_type": (row.get("data_type") or "").strip(),
                "is_nullable": (row.get("is_nullable") or "").strip(),
            }
        )

    return {
        "table_name": table_name,
        "columns": columns,
        "column_details": column_details,
        # raw deliberately omitted — it's the unparsed CSV text and contributes
        # significant bytes to session_state without being read by any agent.
        # Removing it cuts deepcopy cost on the large schema dict (30+ tables).
    }


def prime_connection_from_prompt(run_input: RunInput, run_context: RunContext) -> None:
    """
    Detect a PostgreSQL DSN in the current prompt and store it in session_state.

    This lets AgentOS/playground members build tools from session_state without a
    separate connect tool or global environment mutation.
    """
    state = run_context.session_state
    if state is None:
        return
    if state.get("connection_string"):
        return

    text = _extract_input_text(run_input)
    match = _DSN_PATTERN.search(text)
    if not match:
        return

    candidate = match.group(1).rstrip(".,;")
    if not looks_like_postgres_dsn(candidate):
        return

    try:
        assert_valid_postgres_dsn(candidate)
    except Exception:
        return

    state["connection_string"] = candidate
    logger.info("prime_connection_from_prompt: cached PostgreSQL DSN in session_state")


def mark_run_start(run_context: RunContext) -> None:
    if run_context.session_state is None:
        return
    run_context.session_state["_run_start"] = time.time()
    # Reset the per-agent empty-results streak so each agent starts with a clean slate.
    # Without this, empty results from a previous agent's clean-data checks
    # would carry over and prematurely trigger loop detection for the next agent.
    # The key is scoped per-agent to avoid parallel agents clobbering each other.
    aid = _agent_id(run_context)
    run_context.session_state[f"_empty_results_streak_{aid}"] = 0


def _load_excluded_table_patterns() -> list[str]:
    """Return glob patterns for tables that must be excluded from all analysis."""
    from .rules import domain_rules as load_domain_rules
    rules = load_domain_rules()
    return list(rules.get("excluded_tables", []))


def _is_excluded(table_name: str, patterns: list[str]) -> bool:
    return any(fnmatch.fnmatch(table_name, pat) for pat in patterns)


def _query_all_table_constraints(table_names: list[str]) -> dict[str, dict]:
    """Fetch PK, UNIQUE, index, and FK metadata for all tables in 4 batch queries.

    Replaces the old per-table _query_table_constraints loop, cutting 4×N sequential
    DB connections down to exactly 4 connections regardless of schema size.
    """
    from .tools import _q

    empty: dict = {
        "primary_key_columns": [],
        "has_primary_key": False,
        "unique_constraints": [],
        "indexes": [],
        "indexed_columns": [],
        "foreign_keys": [],
    }
    result: dict[str, dict] = {t: {**empty} for t in table_names}

    # Filter to safe identifiers before embedding in SQL
    safe = [t for t in table_names if _SAFE_IDENTIFIER.match(t)]
    if len(safe) != len(table_names):
        unsafe = sorted(set(table_names) - set(safe))
        logger.warning("_query_all_table_constraints: skipping unsafe table name(s): %s", unsafe)
    if not safe:
        return result

    names_literal = "ARRAY[" + ", ".join(f"'{n}'" for n in safe) + "]"
    limit = len(safe) * 40

    # ── Query 1: PK + UNIQUE constraints ────────────────────────────────────
    raw = _q(f"""
        SELECT tc.table_name, tc.constraint_type, tc.constraint_name,
               array_agg(kcu.column_name ORDER BY kcu.ordinal_position) AS columns
        FROM information_schema.table_constraints tc
        JOIN information_schema.key_column_usage kcu
            ON  tc.constraint_name = kcu.constraint_name
            AND tc.table_schema    = kcu.table_schema
            AND tc.table_name      = kcu.table_name
        WHERE tc.table_schema = 'public'
          AND tc.table_name   = ANY({names_literal}::text[])
          AND tc.constraint_type IN ('PRIMARY KEY', 'UNIQUE')
        GROUP BY tc.table_name, tc.constraint_type, tc.constraint_name
        LIMIT {limit}
    """)
    if raw and not raw.startswith("Error") and not raw.startswith("Query executed"):
        try:
            for row in json.loads(raw):
                tbl = row.get("table_name", "")
                if tbl not in result:
                    continue
                ctype = row.get("constraint_type", "")
                cols = row.get("columns") or []
                if isinstance(cols, str):
                    cols = [c.strip().strip('"') for c in cols.strip("{}").split(",") if c.strip()]
                if ctype == "PRIMARY KEY":
                    result[tbl]["primary_key_columns"] = cols
                    result[tbl]["has_primary_key"] = bool(cols)
                elif ctype == "UNIQUE":
                    result[tbl]["unique_constraints"].append({
                        "name": row.get("constraint_name", ""),
                        "columns": cols,
                    })
        except Exception as exc:
            logger.warning("_query_all_table_constraints: PK/UNIQUE parse error: %s", exc)

    # ── Query 2: Index names ─────────────────────────────────────────────────
    raw = _q(f"""
        SELECT tablename, indexname FROM pg_indexes
        WHERE schemaname = 'public'
          AND tablename = ANY({names_literal}::text[])
        LIMIT {limit}
    """)
    if raw and not raw.startswith("Error") and not raw.startswith("Query executed"):
        try:
            for row in json.loads(raw):
                tbl = row.get("tablename", "")
                idx = row.get("indexname", "")
                if tbl in result and idx:
                    result[tbl]["indexes"].append(idx)
        except Exception as exc:
            logger.warning("_query_all_table_constraints: index names parse error: %s", exc)

    # ── Query 3: Indexed columns ─────────────────────────────────────────────
    raw = _q(f"""
        SELECT DISTINCT t.relname AS table_name, a.attname AS column_name
        FROM pg_class t
        JOIN pg_index ix ON t.oid = ix.indrelid
        JOIN pg_attribute a ON a.attrelid = t.oid AND a.attnum = ANY(ix.indkey)
        JOIN pg_namespace n ON n.oid = t.relnamespace
        WHERE n.nspname = 'public'
          AND t.relname = ANY({names_literal}::text[])
        LIMIT {limit}
    """)
    if raw and not raw.startswith("Error") and not raw.startswith("Query executed"):
        try:
            for row in json.loads(raw):
                tbl = row.get("table_name", "")
                col = row.get("column_name", "")
                if tbl in result and col:
                    result[tbl]["indexed_columns"].append(col)
        except Exception as exc:
            logger.warning("_query_all_table_constraints: indexed columns parse error: %s", exc)

    # ── Query 4: FK definitions ──────────────────────────────────────────────
    raw = _q(f"""
        SELECT
            tc.table_name,
            tc.constraint_name,
            kcu.column_name,
            fk_target.table_name  AS foreign_table,
            fk_target.column_name AS foreign_column
        FROM information_schema.table_constraints tc
        JOIN information_schema.key_column_usage kcu
            ON  tc.constraint_name = kcu.constraint_name
            AND tc.table_schema    = kcu.table_schema
            AND tc.table_name      = kcu.table_name
        LEFT JOIN information_schema.referential_constraints rc
            ON  tc.constraint_name  = rc.constraint_name
            AND tc.table_schema     = rc.constraint_schema
        LEFT JOIN information_schema.key_column_usage fk_target
            ON  rc.unique_constraint_name   = fk_target.constraint_name
            AND rc.unique_constraint_schema = fk_target.constraint_schema
            AND kcu.ordinal_position        = fk_target.ordinal_position
        WHERE tc.table_schema    = 'public'
          AND tc.table_name      = ANY({names_literal}::text[])
          AND tc.constraint_type = 'FOREIGN KEY'
        ORDER BY tc.table_name, kcu.ordinal_position
        LIMIT {limit}
    """)
    if raw and not raw.startswith("Error") and not raw.startswith("Query executed"):
        try:
            for row in json.loads(raw):
                tbl = row.get("table_name", "")
                col = row.get("column_name", "")
                ftbl = row.get("foreign_table", "")
                if tbl not in result or not col or not ftbl:
                    continue
                result[tbl]["foreign_keys"].append({
                    "constraint_name": row.get("constraint_name", ""),
                    "column":          col,
                    "foreign_table":   ftbl,
                    "foreign_column":  row.get("foreign_column", ""),
                })
        except Exception as exc:
            logger.warning("_query_all_table_constraints: FK parse error: %s", exc)

    return result


def ensure_schema_primed(run_input: RunInput, run_context: RunContext) -> None:
    """
    Populate session_state["schema"] from PostgresTools.show_tables/describe_table.

    Tables matching excluded_tables patterns in domain_rules are filtered out here
    so no agent sees or analyses them — enforcement at the data level, not prompt level.

    In workflow mode we require a database connection. In playground mode we only
    attempt priming when a DSN is already available.
    """
    state = run_context.session_state
    if state is None:
        return
    if state.get("schema"):
        return

    pg = build_postgres_tools(
        include=["show_tables", "describe_table"],
        session_state=state,
        run_context=run_context,
    )
    if pg is None:
        if state.get("require_database"):
            raise InputCheckError(
                "No PostgreSQL connection could be resolved for this run.",
                check_trigger=CheckTrigger.INPUT_NOT_ALLOWED,
            )
        return

    # Connect postgres_tool early so _query_pk_columns can run below.
    connection_string = state.get("connection_string")
    if connection_string:
        from .tools import postgres_tool
        postgres_tool.connect(connection_string)

    excluded_patterns = _load_excluded_table_patterns()
    all_tables = _normalize_table_list_result(pg.show_tables())
    tables = [t for t in all_tables if not _is_excluded(t, excluded_patterns)]

    if len(all_tables) != len(tables):
        skipped = sorted(set(all_tables) - set(tables))
        logger.info("ensure_schema_primed: excluded %d table(s): %s", len(skipped), skipped)

    # Fetch PK/UNIQUE/index/FK data for all tables in 4 batch queries instead of 4×N.
    all_constraints = _query_all_table_constraints(tables) if connection_string else {}

    schema: dict[str, Any] = {}
    for table_name in tables:
        try:
            entry = _normalize_describe_table_result(
                table_name,
                pg.describe_table(table_name),
            )
            if connection_string:
                entry.update(all_constraints.get(table_name, {}))
            schema[table_name] = entry
        except Exception as exc:
            logger.warning("ensure_schema_primed: failed to describe %s: %s", table_name, exc)

    state["schema"] = schema

    # Compact schema for the profiler: only (column_name, data_type) — no constraints.
    # Reduces context by ~60% and prevents MALFORMED_FUNCTION_CALL on large schemas.
    state["profiler_schema"] = {
        table_name: {
            "column_details": [
                {"column_name": c["column_name"], "data_type": c["data_type"]}
                for c in entry.get("column_details", [])
            ]
        }
        for table_name, entry in schema.items()
    }

    # Compact schema for statistical and structural validators: column_details +
    # unique_constraints only. Strips FK/index metadata (~40% of the full schema)
    # that these agents never need, reducing their input token cost.
    state["validator_schema"] = {
        table_name: {
            "columns": entry.get("columns", []),
            "column_details": entry.get("column_details", []),
            "unique_constraints": entry.get("unique_constraints", []),
        }
        for table_name, entry in schema.items()
    }

    # Compact schema for the relational validator: keeps FK list (needed for orphan sweep
    # Phase 1) and unique_constraints (for duplicate threshold check), strips index metadata.
    state["relational_schema"] = {
        table_name: {
            "columns":            entry.get("columns", []),
            "column_details":     entry.get("column_details", []),
            "has_primary_key":    entry.get("has_primary_key", False),
            "primary_key_columns": entry.get("primary_key_columns", []),
            "unique_constraints": entry.get("unique_constraints", []),
            "foreign_keys":       entry.get("foreign_keys", []),
        }
        for table_name, entry in schema.items()
    }

    # Compact schema for the fix planner: keeps column types and PK/UNIQUE facts
    # needed for DDL generation; strips FK list (planner uses describe_constraints()
    # for FK discovery) and index metadata.
    state["fix_planner_schema"] = {
        table_name: {
            "columns":             entry.get("columns", []),
            "column_details":      entry.get("column_details", []),
            "has_primary_key":     entry.get("has_primary_key", False),
            "primary_key_columns": entry.get("primary_key_columns", []),
            "unique_constraints":  entry.get("unique_constraints", []),
        }
        for table_name, entry in schema.items()
    }

    logger.info("ensure_schema_primed: loaded %d tables", len(schema))


def inject_domain_rules(run_input: RunInput, run_context: RunContext) -> None:
    deps = run_context.dependencies or {}
    rules = deps.get("domain_rules")

    if callable(rules):
        rules = rules()
        deps["domain_rules"] = rules
        run_context.dependencies = deps

    if rules is None:
        deps["domain_rules"] = {}
        run_context.dependencies = deps
        return

    if not isinstance(rules, dict):
        raise InputCheckError(
            f"domain_rules must resolve to a dict, got {type(rules).__name__}.",
            check_trigger=CheckTrigger.INPUT_NOT_ALLOWED,
        )

    missing = {"max_null_pct", "naming_convention"} - set(rules.keys())
    if missing:
        raise InputCheckError(
            f"domain_rules is missing required keys: {sorted(missing)}.",
            check_trigger=CheckTrigger.INPUT_NOT_ALLOWED,
        )


def validate_findings_entities(run_output: RunOutput, run_context: RunContext) -> None:
    """Strip findings/cards that reference non-existent schema entities.

    Filters in-place rather than raising OutputCheckError, because the retry
    mechanism is ineffective — the model reliably re-hallucinates the same
    ghost entities on every attempt.
    """
    content = run_output.content
    if not isinstance(content, (ValidationReport, FixPlan)):
        return

    schema: dict[str, Any] = (run_context.session_state or {}).get("schema") or {}
    if not schema:
        logger.warning("validate_findings_entities: schema unavailable, skipping")
        return

    valid_tables = set(schema.keys()) | {"general", "database"}
    valid_pairs = {
        f"{table}.{column}"
        for table, info in schema.items()
        for column in info.get("columns", [])
    }

    def _valid(entity: str) -> bool:
        return entity in valid_tables or entity in valid_pairs

    if isinstance(content, ValidationReport):
        before = len(content.findings)
        content.findings = [f for f in content.findings if _valid(f.entity)]
        dropped = before - len(content.findings)
        if dropped:
            logger.warning(
                "validate_findings_entities: stripped %d finding(s) with non-existent entities",
                dropped,
            )

        # FK-rule guard: foreign_key_validation, orphaned_fk, and index_gap are only valid
        # for columns that are actual FK constraints in the schema. Strip inferred-FK findings
        # (LLM hallucinating FKs from naming patterns like contact_id → contacts).
        if content.validator in ("schema", "relational"):
            _fk_gated_rules = {"foreign_key_validation", "orphaned_fk", "index_gap"}
            before_fk = len(content.findings)

            def _column_is_actual_fk(finding) -> bool:
                if finding.rule not in _fk_gated_rules:
                    return True
                parts = finding.entity.split(".", 1)
                if len(parts) != 2:
                    return True
                tbl, col = parts
                fk_cols = {fk.get("column") for fk in (schema.get(tbl) or {}).get("foreign_keys", [])}
                return col in fk_cols

            content.findings = [f for f in content.findings if _column_is_actual_fk(f)]
            dropped_fk = before_fk - len(content.findings)
            if dropped_fk:
                logger.warning(
                    "validate_findings_entities: stripped %d FK-rule finding(s) for non-FK columns",
                    dropped_fk,
                )

        # Statistical findings must have a profiler-measured anchor. Strip any that
        # reference a table the profiler didn't cover — catches hallucinations that
        # slip past the agent-level scope guard before the reducer's 3d filter runs.
        profiler_tables: set[str] = set(
            (run_context.session_state or {}).get("profiler_tables") or []
        )
        if content.validator == "statistical" and profiler_tables:
            before_cov = len(content.findings)
            content.findings = [
                f for f in content.findings
                if f.entity.split(".")[0] in profiler_tables | {"general", "database"}
            ]
            dropped_cov = before_cov - len(content.findings)
            if dropped_cov:
                logger.warning(
                    "validate_findings_entities: stripped %d statistical finding(s) "
                    "for tables outside profiler coverage",
                    dropped_cov,
                )
    else:
        before = len(content.cards)
        content.cards = [c for c in content.cards if _valid(c.target)]
        dropped = before - len(content.cards)
        if dropped:
            logger.warning(
                "validate_findings_entities: stripped %d fix card(s) with non-existent entities",
                dropped,
            )


def store_profiler_output(run_output: RunOutput, run_context: RunContext) -> None:
    """Serialize ProfilerReport into session_state so parallel validators can reference it.

    Full dump stored under "profiler_report" for backward compat.
    Compact summary stored under "profiler_summary" — agents should use this.
    The compact form strips verbose numeric stats (mean, stddev, percentiles, top_values)
    that add ~18,000 tokens to every agent context without adding decision value.
    """
    content = run_output.content
    if not isinstance(content, ProfilerReport):
        return
    try:
        if run_context.session_state is None:
            return
        run_context.session_state["profiler_report"] = content.model_dump()
        run_context.session_state["profiler_summary"] = {
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
            for t in content.tables
        }
        # Pre-computed table name list so the reducer doesn't need to re-derive it
        # from the full profiler_report (avoids re-deserializing a large dict).
        run_context.session_state["profiler_tables"] = [t.table_name for t in content.tables]
        logger.info("store_profiler_output: profiled %d table(s)", len(content.tables))
    except Exception as exc:
        logger.warning("store_profiler_output: failed to serialize profiler report: %s", exc)


_AGENT_STEP_STATUS: dict[str, str] = {
    "schema-validator":      "running_schema",
    "profiler":              "running_profiler",
    "statistical-validator": "running_statistical",
    "structural-validator":  "running_structural",
    "relational-validator":  "running_relational",
    "fix-planner":           "running_fix_planner",
}


def emit_pipeline_status(run_input: RunInput, run_context: RunContext) -> None:
    """Before each pipeline agent runs, push the matching job status to the DB.

    The loading screen polls /api/status/:jobId every 5 s and advances the
    step indicator when the status changes.  Requires job_id in session_state
    (set by orchestrator_workflow via _initial_session_state).
    """
    agent_id = getattr(getattr(run_context, "agent", None), "id", "") or ""
    step_status = _AGENT_STEP_STATUS.get(agent_id)
    if not step_status:
        return

    job_id = (run_context.session_state or {}).get("job_id")
    if not job_id:
        return

    try:
        from api.db import update_job_status
        update_job_status(job_id, step_status)
        logger.debug("emit_pipeline_status: %s → %s", agent_id, step_status)
    except Exception as exc:
        logger.warning("emit_pipeline_status: failed to update job status for %s: %s", agent_id, exc)
    try:
        from asgiref.sync import async_to_sync
        from channels.layers import get_channel_layer
        channel_layer = get_channel_layer()
        if channel_layer is not None:
            async_to_sync(channel_layer.group_send)(
                f"job_{job_id}",
                {"type": "job.status", "status": step_status},
            )
    except Exception as exc:
        logger.debug("emit_pipeline_status: WebSocket push skipped: %s", exc)

def log_run_metrics(run_output: RunOutput, run_context: RunContext) -> None:
    state = run_context.session_state or {}
    elapsed_ms = (time.time() - state.get("_run_start", time.time())) * 1000
    aid = _agent_id(run_context)
    tool_calls = len(state.get(f"_tool_calls_seen_{aid}", {}))

    findings_count = 0
    if isinstance(run_output.content, ValidationReport):
        findings_count = len(run_output.content.findings)
    elif isinstance(run_output.content, FixPlan):
        findings_count = len(run_output.content.cards)

    logger.info(
        "run_complete agent=%s ms=%.0f tool_calls=%d findings=%d",
        getattr(getattr(run_context, "agent", None), "name", "team"),
        elapsed_ms,
        tool_calls,
        findings_count,
    )


try:
    import sqlglot

    _SQLGLOT_AVAILABLE = True
except ImportError:
    _SQLGLOT_AVAILABLE = False
    logger.warning("sqlglot not installed; validate_fix_sql_safety will be skipped")


_ALLOWED_STATEMENT_TYPES = {
    "Select",
    "Update",
    "Insert",
    "Delete",
    "Create",
    "Alter",
    "Comment",
    "AlterTable",
    "CreateIndex",
    "CreateTable",
    # sqlglot structural wrappers that are not destructive
    "Union",
    "Intersect",
    "Except",
    "With",
    "Subquery",
}
_BLOCKED_STATEMENT_TYPES = {"Drop", "Truncate", "DropTable"}


def validate_fix_sql_safety(run_output: RunOutput) -> None:
    plan = run_output.content
    if not isinstance(plan, FixPlan):
        return
    if not _SQLGLOT_AVAILABLE:
        return

    violations: list[str] = []
    for card in plan.cards:
        sql_text = card.sql or ""
        if not sql_text.strip():
            continue
        try:
            statements = sqlglot.parse(sql_text, dialect="postgres")
        except Exception as exc:
            violations.append(f"{card.target}: unparseable SQL ({exc})")
            continue

        for statement in statements:
            if statement is None:
                continue
            statement_type = type(statement).__name__
            if statement_type in _BLOCKED_STATEMENT_TYPES:
                violations.append(f"{card.target}: forbidden statement type '{statement_type}'")
            elif statement_type not in _ALLOWED_STATEMENT_TYPES:
                # sqlglot falls back to 'Command' for valid DDL it cannot fully parse.
                # Whitelist known-safe patterns before rejecting:
                #   • Standard DDL prefixes (ALTER TABLE, CREATE INDEX, etc.)
                #   • DO $$ ... $$ PL/pgSQL blocks — used for idempotent constraint adds
                #     (e.g. IF NOT EXISTS guards). sqlglot cannot parse anonymous blocks.
                if statement_type == "Command":
                    raw_upper = sql_text.strip().upper()
                    # Normalize internal whitespace for prefix matching so
                    # "DO $$", "DO  $$", "DO\t$$" all match as well as "DO$$".
                    raw_upper_ws = " ".join(raw_upper.split())
                    _safe_ddl = ("ALTER TABLE", "CREATE INDEX", "CREATE TABLE", "COMMENT ON")
                    if (
                        any(raw_upper.startswith(p) for p in _safe_ddl)
                        or raw_upper_ws.startswith("DO $$")
                        or raw_upper_ws.startswith("DO$$")
                    ):
                        continue
                violations.append(f"{card.target}: unexpected statement type '{statement_type}'")

    if violations:
        raise OutputCheckError(
            f"FixPlan contains unsafe SQL: {violations[:5]}",
            check_trigger=CheckTrigger.OUTPUT_NOT_ALLOWED,
        )
