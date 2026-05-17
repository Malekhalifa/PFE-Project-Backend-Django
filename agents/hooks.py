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


_REDIS_PROBE_LOGGED = False


def _redis_client():
    """Return a Redis client if REDIS_URL resolves, else None.

    On first call per process, log once whether the connection succeeded or
    why it failed. Without this probe, all downstream Redis users (tool cache,
    tool-count side-channel) silently degrade with no signal in logs.
    """
    global _REDIS_PROBE_LOGGED
    try:
        import redis as _redis
        url = os.environ.get("REDIS_URL", "redis://localhost:6379/0")
        client = _redis.from_url(url, decode_responses=True, socket_connect_timeout=1)
        if not _REDIS_PROBE_LOGGED:
            _REDIS_PROBE_LOGGED = True
            try:
                client.ping()
                logger.info("redis: connected to %s (probe ok)", url)
            except Exception as ping_exc:
                logger.warning(
                    "redis: connect to %s returned client but ping failed: %s "
                    "(tool-cache + audit fallback degraded)",
                    url, ping_exc,
                )
        return client
    except Exception as exc:
        if not _REDIS_PROBE_LOGGED:
            _REDIS_PROBE_LOGGED = True
            logger.warning(
                "redis: client init failed (REDIS_URL=%s): %s "
                "(tool-cache + audit fallback degraded)",
                os.environ.get("REDIS_URL", "redis://localhost:6379/0"), exc,
            )
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

    # ── Activity counter — bump on EVERY tool hook entry, regardless of cache.
    # Reads by reducer's silent-failure audit. Must fire even when L1/L2 cache
    # hits return early below; otherwise an agent that legitimately did 25
    # queries (all cache-hit) reports tool_calls=0 → audit thinks the agent
    # silently failed when it actually worked normally.
    counts = run_context.session_state.setdefault("_validator_tool_calls", {})
    counts[aid] = int(counts.get(aid, 0)) + 1
    _new_count = counts[aid]

    # Mirror to Redis side-channel (bypasses the agent→workflow session
    # propagation gap — reducer reads this when in-memory dict missing).
    _r_counter = _redis_client()
    if _r_counter is not None and job_id != "nojob":
        try:
            _r_counter.setex(f"ql:tcounts:{job_id}:{aid}", 3600, str(_new_count))
        except Exception as _exc:
            _key = f"_redis_tcounts_warned_{aid}"
            if not run_context.session_state.get(_key):
                run_context.session_state[_key] = True
                logger.warning(
                    "tool_hook: Redis setex for tcounts failed (aid=%s): %s",
                    aid, _exc,
                )

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
    # NOTE: _validator_tool_calls + Redis tcounts are bumped at the TOP of the
    # hook (so cache hits also count). No double-write needed here.

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
    result: dict[str, dict] = {
        t: {
            "primary_key_columns": [],
            "has_primary_key": False,
            "unique_constraints": [],
            "indexes": [],
            "indexed_columns": [],
            "foreign_keys": [],
        }
        for t in table_names
    }

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

    _prime_t0 = time.perf_counter()

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
    # unique_constraints only. Strips columns list (derivable from column_details),
    # FK/index metadata that these agents never need.
    state["validator_schema"] = {
        table_name: {
            "column_details": entry.get("column_details", []),
            "unique_constraints": entry.get("unique_constraints", []),
        }
        for table_name, entry in schema.items()
    }

    # Compact schema for the relational validator: keeps FK list (needed for orphan sweep
    # Phase 1) and unique_constraints (for duplicate threshold check), strips index metadata
    # and redundant columns list.
    state["relational_schema"] = {
        table_name: {
            "column_details":      entry.get("column_details", []),
            "has_primary_key":     entry.get("has_primary_key", False),
            "primary_key_columns": entry.get("primary_key_columns", []),
            "unique_constraints":  entry.get("unique_constraints", []),
            "foreign_keys":        entry.get("foreign_keys", []),
        }
        for table_name, entry in schema.items()
    }

    # Compact schema for the fix planner: keeps column types and PK/UNIQUE facts
    # needed for DDL generation; strips FK list (planner uses describe_constraints()
    # for FK discovery), index metadata, and redundant columns list.
    state["fix_planner_schema"] = {
        table_name: {
            "column_details":      entry.get("column_details", []),
            "has_primary_key":     entry.get("has_primary_key", False),
            "primary_key_columns": entry.get("primary_key_columns", []),
            "unique_constraints":  entry.get("unique_constraints", []),
        }
        for table_name, entry in schema.items()
    }

    elapsed_ms = round((time.perf_counter() - _prime_t0) * 1000)
    timing = state.setdefault("_phase_timing_ms", {})
    timing["schema_priming"] = elapsed_ms

    logger.info("ensure_schema_primed: loaded %d tables", len(schema))


# Per-agent slim domain_rules key sets. Each agent only receives the keys it
# actually uses — cuts ~50% of domain_rules injection tokens per call.
_SCHEMA_RULES_KEYS: frozenset[str] = frozenset({
    "naming_convention", "primary_key_required", "foreign_key_validation",
    "nullable_fk_by_design", "excluded_tables",
})
_STATISTICAL_RULES_KEYS: frozenset[str] = frozenset({
    "max_null_pct", "unique_column_patterns", "min_rows_for_cardinality",
    "lookup_table_patterns", "excluded_low_cardinality_columns",
    "duplicate_check_scope", "null_thresholds",
})
_STRUCTURAL_RULES_KEYS: frozenset[str] = frozenset({
    "max_violation_row_pct", "custom_rules", "null_thresholds",
})
_RELATIONAL_RULES_KEYS: frozenset[str] = frozenset({
    "unique_column_patterns", "duplicate_check_scope",
    "nullable_fk_by_design", "foreign_key_validation",
})
_FIX_PLANNER_RULES_KEYS: frozenset[str] = frozenset({
    "max_violation_row_pct", "nullable_fk_by_design",
    "custom_rules", "foreign_key_validation",
})


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

    state = run_context.session_state
    if state is not None:
        state["schema_rules"]      = {k: rules[k] for k in _SCHEMA_RULES_KEYS      if k in rules}
        state["statistical_rules"] = {k: rules[k] for k in _STATISTICAL_RULES_KEYS if k in rules}
        state["structural_rules"]  = {k: rules[k] for k in _STRUCTURAL_RULES_KEYS  if k in rules}
        state["relational_rules"]  = {k: rules[k] for k in _RELATIONAL_RULES_KEYS  if k in rules}
        state["fix_planner_rules"] = {k: rules[k] for k in _FIX_PLANNER_RULES_KEYS if k in rules}

        custom = rules.get("custom_rules") or []
        if custom:
            logger.info(
                "inject_domain_rules: %d custom rule(s) active (visible to Structural Validator only): %s",
                len(custom),
                [str(r)[:80] for r in custom],
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
        # row_count only — structural agent uses this for table ordering (descending).
        # Avoids injecting full profiler stats into the structural agent context.
        run_context.session_state["structural_profiler_summary"] = {
            t.table_name: t.row_count for t in content.tables
        }
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
        logger.info(
            "pipeline_status: %s job=%s source=agent_pre_hook(%s)",
            step_status, job_id, agent_id,
        )
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

_AGENT_ID_TO_PHASE = {
    "schema-validator": "Schema Integrity Validator",
    "profiler": "Database Profiler",
    "statistical-validator": "Statistical Validator",
    "structural-validator": "Structural Validator",
    "relational-validator": "Relational Validator",
    "fix-planner": "Data Fix Planner",
}


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

    agent = getattr(run_context, "agent", None)
    phase_name = (
        _AGENT_ID_TO_PHASE.get(aid)
        or getattr(agent, "name", None)
        or "team"
    )

    timing = state.setdefault("_phase_timing_ms", {})
    timing[phase_name] = round(elapsed_ms)

    # Stable per-agent tool-call counter the reducer can audit. The raw
    # `_tool_calls_seen_<aid>` dicts may not survive the agent→workflow session
    # propagation gap, so we record an integer count keyed by agent id under a
    # well-known key that the reducer's session_data view can read reliably.
    tool_call_counts = state.setdefault("_validator_tool_calls", {})
    tool_call_counts[aid] = tool_calls

    logger.info(
        "run_complete agent=%s ms=%.0f tool_calls=%d findings=%d",
        phase_name,
        elapsed_ms,
        tool_calls,
        findings_count,
    )

    # Extract token counts from Agno's RunOutput metrics (populated by the Gemini backend).
    # Agno stores accumulated token usage on RunMetrics; some versions use int, some list.
    metrics = getattr(run_output, "metrics", None)
    input_tokens: int | None = getattr(metrics, "input_tokens", None)
    output_tokens: int | None = getattr(metrics, "output_tokens", None)
    if isinstance(input_tokens, list):
        input_tokens = sum(x for x in input_tokens if isinstance(x, int)) or None
    if isinstance(output_tokens, list):
        output_tokens = sum(x for x in output_tokens if isinstance(x, int)) or None

    try:
        from .langfuse_tracer import create_agent_span
        create_agent_span(
            agent_name=phase_name,
            elapsed_ms=elapsed_ms,
            tool_calls=tool_calls,
            findings_count=findings_count,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            model_id=state.get("model_id"),
        )
    except Exception as _lf_exc:
        logger.debug("Langfuse span skipped: %s", _lf_exc)


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
                # Whitelist known-safe standard DDL prefixes before rejecting.
                if statement_type == "Command":
                    # Render the individual statement back to SQL so the prefix check
                    # works in multi-statement batches.
                    try:
                        stmt_text = statement.sql(dialect="postgres")
                    except Exception:
                        stmt_text = str(statement)
                    stmt_upper_ws = " ".join(stmt_text.strip().upper().split())
                    _safe_ddl = ("ALTER TABLE", "CREATE INDEX", "CREATE TABLE", "COMMENT ON")
                    if any(stmt_upper_ws.startswith(p) for p in _safe_ddl):
                        continue
                violations.append(f"{card.target}: unexpected statement type '{statement_type}'")

    if violations:
        raise OutputCheckError(
            f"FixPlan contains unsafe SQL: {violations[:5]}",
            check_trigger=CheckTrigger.OUTPUT_NOT_ALLOWED,
        )


# ─────────────────────────────────────────────────────────────────────────────
# Pre-flight data-safety guard for fix cards
# ─────────────────────────────────────────────────────────────────────────────
# Pattern matches for the SQL shapes the LLM emits. Conservative — only the
# common forms we want to inspect; anything else passes through unchanged.

_RE_ADD_PK = re.compile(
    r"ALTER\s+TABLE\s+(?:ONLY\s+)?(?:\"?(\w+)\"?\.)?\"?(\w+)\"?\s+"
    r"ADD\s+(?:CONSTRAINT\s+\"?\w+\"?\s+)?PRIMARY\s+KEY\s*\(\s*([^)]+?)\s*\)\s*;?",
    re.IGNORECASE,
)
_RE_ADD_UNIQUE = re.compile(
    r"ALTER\s+TABLE\s+(?:ONLY\s+)?(?:\"?(\w+)\"?\.)?\"?(\w+)\"?\s+"
    r"ADD\s+CONSTRAINT\s+\"?(\w+)\"?\s+UNIQUE\s*\(\s*([^)]+?)\s*\)\s*;?",
    re.IGNORECASE,
)
_RE_SET_NOT_NULL = re.compile(
    r"ALTER\s+TABLE\s+(?:ONLY\s+)?(?:\"?\w+\"?\.)?\"?(\w+)\"?\s+"
    r"ALTER\s+COLUMN\s+\"?(\w+)\"?\s+SET\s+NOT\s+NULL\s*;?",
    re.IGNORECASE,
)
_RE_UPDATE_FK_PLACEHOLDER = re.compile(
    r"UPDATE\s+(?:\"?\w+\"?\.)?\"?(\w+)\"?\s+SET\s+\"?(\w+)\"?\s*=\s*(\d+)\s+"
    r"WHERE\s+\"?\2\"?\s+IS\s+NULL\s*;?",
    re.IGNORECASE,
)


def _split_cols(cols_text: str) -> list[str]:
    return [c.strip().strip('"') for c in cols_text.split(",") if c.strip()]


def _conn_str_from_state(state: dict) -> str | None:
    return state.get("connection_string") or state.get("dsn")


def _safe_query(conn_str: str, sql: str) -> Any:
    """Run a single read-only SQL via psycopg2. Returns first column of first row,
    or None on failure. Wrapped tightly so any DB error degrades to 'cannot prove safe'.
    """
    import psycopg2
    conn = None
    try:
        conn = psycopg2.connect(conn_str, connect_timeout=5)
        with conn.cursor() as cur:
            cur.execute("SET statement_timeout = 5000;")
            cur.execute(sql)
            row = cur.fetchone()
            return row[0] if row else None
    except Exception as exc:
        logger.warning("preflight: query failed (%s): %s", sql[:80], exc)
        return None
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


def _block_card(card: Any, reason: str) -> None:
    """Mark a card as blocked and remove executable SQL from the fix payload."""
    original = card.sql or ""
    card.sql = ""
    card.risk_level = "medium"
    if hasattr(card, "interaction_model"):
        card.interaction_model = "blocked"
    if hasattr(card, "card_family") and getattr(card, "card_family", "unknown") == "unknown":
        card.card_family = "nullability" if "placeholder" in reason.lower() else "schema_design"
    if hasattr(card, "source_rule") and not getattr(card, "source_rule", ""):
        card.source_rule = "fill_nulls_blocked"
    if hasattr(card, "evidence_payload"):
        card.evidence_payload = {
            "_type": "blocked",
            "block_reason": reason,
            "original_sql": original,
        }
    if hasattr(card, "evidence"):
        evidence = dict(getattr(card, "evidence", {}) or {})
        evidence.update({"block_reason": reason, "original_sql": original})
        card.evidence = evidence
    if hasattr(card, "rationale"):
        card.rationale = (
            f"[Pre-flight blocked: {reason}] " + (card.rationale or "")
        ).strip()
    if hasattr(card, "preconditions"):
        card.preconditions = list(card.preconditions or []) + [
            f"BLOCKED: {reason}",
            f"Original SQL: {original[:200]}",
        ]


def _prepend_set_not_null(card: Any, table: str, columns: list[str]) -> None:
    """Insert ALTER COLUMN ... SET NOT NULL statements before the existing SQL."""
    prefix = " ".join(
        f"ALTER TABLE {table} ALTER COLUMN {c} SET NOT NULL;" for c in columns
    )
    card.sql = f"{prefix} {card.sql}".strip()
    if hasattr(card, "rationale"):
        card.rationale = (
            f"[Pre-flight: prepended SET NOT NULL on {', '.join(columns)} — required for PK] "
            + (card.rationale or "")
        ).strip()


def _is_pattern_not_unique_card(card: Any) -> bool:
    """Detect cards derived from the noisy `pattern_not_unique` schema rule.
    Pattern matches like *_code, *_type fire even on country codes / enum types
    that legitimately repeat — promoting them to UNIQUE constraints destroys data.
    """
    rationale = (getattr(card, "rationale", "") or "").lower()
    title = (getattr(card, "title", "") or "").lower()
    needles = ("pattern_not_unique", "matches a unique pattern", "matches a unique column pattern")
    return any(n in rationale or n in title for n in needles)


def validate_fix_preflight(run_output: RunOutput, run_context: RunContext) -> None:
    """Pre-flight data-safety guard. Runs after the LLM emits a FixPlan and after
    SQL syntax validation. For each card, queries the live database to verify
    that the proposed SQL will not destroy data or fail at execution time.

    Five guards:
      1. ADD PRIMARY KEY — verify the column(s) have no NULLs and no duplicates.
         Auto-prepend SET NOT NULL when nullable but unique. Block when duplicate.
      2. ADD CONSTRAINT ... UNIQUE — verify column has no duplicate non-null values.
         Block when duplicates exist (constraint creation would fail anyway, and
         the LLM sometimes prepends a DELETE that would destroy real data).
      3. UPDATE <fk_col> = <N> WHERE <fk_col> IS NULL — verify N exists in the
         FK target table. Block when target row absent (creates orphaned FK).
      4. Cards derived from `pattern_not_unique` rule — block outright. The
         schema-rule's `*_code` / `*_type` regex is too noisy to safely promote
         to a UNIQUE constraint without human review.
      5. (Inherited from upstream hooks) entity existence + SQL syntax safety.

    Failure mode: if the database is unreachable, all queries return None and
    cards pass through untouched (we can't prove unsafe → don't break the run).
    Blocked cards keep their slot in the FixPlan so the user sees what was
    attempted, but their SQL is replaced with an inert comment.
    """
    plan = run_output.content
    if not isinstance(plan, FixPlan):
        return

    state = run_context.session_state or {}
    conn_str = _conn_str_from_state(state)
    if not conn_str:
        # Try the executor's stored DSN as fallback.
        try:
            from .tools import postgres_tool
            conn_str = postgres_tool.connection_string
        except Exception:
            conn_str = None
    if not conn_str:
        logger.warning("preflight: no connection_string available — skipping all checks")
        return

    blocked_count = 0
    modified_count = 0
    for card in plan.cards:
        sql_text = card.sql or ""
        if not sql_text.strip():
            continue

        # Guard 4: pattern_not_unique advisory — block before any DB query.
        if _is_pattern_not_unique_card(card):
            _block_card(
                card,
                "rule 'pattern_not_unique' is advisory; promoting *_code/*_type to UNIQUE "
                "destroys legitimate enum-like values (country codes, status types)",
            )
            blocked_count += 1
            continue

        # Guard 1: PK addition. Match the first occurrence; multi-statement
        # cards may chain SET NOT NULL + ADD PK so we only inspect the PK part.
        m_pk = _RE_ADD_PK.search(sql_text)
        if m_pk:
            table = m_pk.group(2)
            cols = _split_cols(m_pk.group(3))
            if not cols:
                continue

            # Skip the NULL check for columns the SAME card already declared NOT NULL.
            already_set_not_null: set[str] = set()
            for m_snn in _RE_SET_NOT_NULL.finditer(sql_text):
                if m_snn.group(1).lower() == table.lower():
                    already_set_not_null.add(m_snn.group(2).lower())

            cols_to_check = [c for c in cols if c.lower() not in already_set_not_null]
            null_violators: list[str] = []
            for col in cols_to_check:
                cnt = _safe_query(
                    conn_str,
                    f"SELECT COUNT(*) FROM {table} WHERE {col} IS NULL",
                )
                if cnt is not None and cnt > 0:
                    null_violators.append(col)

            cols_csv = ", ".join(cols)
            dup_count = _safe_query(
                conn_str,
                f"SELECT COUNT(*) - COUNT(DISTINCT ({cols_csv})) FROM {table} WHERE "
                + " AND ".join(f"{c} IS NOT NULL" for c in cols),
            )
            if dup_count is not None and dup_count > 0:
                _block_card(
                    card,
                    f"PK on ({cols_csv}) on table '{table}' would fail: "
                    f"{dup_count} duplicate row(s) found",
                )
                blocked_count += 1
                continue

            if null_violators:
                _prepend_set_not_null(card, table, null_violators)
                modified_count += 1
            continue

        # Guard 2: UNIQUE constraint addition.
        m_uq = _RE_ADD_UNIQUE.search(sql_text)
        if m_uq:
            table = m_uq.group(2)
            cols = _split_cols(m_uq.group(4))
            if not cols:
                continue
            cols_csv = ", ".join(cols)
            dup_count = _safe_query(
                conn_str,
                f"SELECT COUNT(*) - COUNT(DISTINCT ({cols_csv})) FROM {table} WHERE "
                + " AND ".join(f"{c} IS NOT NULL" for c in cols),
            )
            if dup_count is not None and dup_count > 0:
                _block_card(
                    card,
                    f"UNIQUE on ({cols_csv}) on table '{table}' would fail: "
                    f"{dup_count} duplicate row(s) found",
                )
                blocked_count += 1
            continue

        # Guard 3: UPDATE fk_col = N WHERE fk_col IS NULL — verify FK target exists.
        m_upd = _RE_UPDATE_FK_PLACEHOLDER.search(sql_text)
        if m_upd:
            src_table = m_upd.group(1)
            fk_col = m_upd.group(2)
            placeholder = int(m_upd.group(3))
            schema_map = state.get("schema") or {}
            tbl_meta = schema_map.get(src_table) or {}
            fks = tbl_meta.get("foreign_keys") or []
            target_table = None
            target_col = None
            for fk in fks:
                if (fk.get("column") or "").lower() == fk_col.lower():
                    target_table = fk.get("foreign_table") or fk.get("references_table")
                    target_col = fk.get("foreign_column") or fk.get("references_column")
                    break
            # Without confirmed FK target table AND target column from the
            # information_schema-derived metadata, we have no way to verify the
            # placeholder is a valid FK target. Don't guess (no '_id' → 'id'
            # convention assumption) — leave the card alone.
            if not target_table or not target_col:
                continue
            exists = _safe_query(
                conn_str,
                f"SELECT EXISTS(SELECT 1 FROM {target_table} WHERE {target_col} = {placeholder})",
            )
            if exists is False or exists == 0:
                _block_card(
                    card,
                    f"placeholder {fk_col}={placeholder} does not exist in "
                    f"{target_table}({target_col}); would create orphan FK references",
                )
                blocked_count += 1
            continue

    if blocked_count or modified_count:
        logger.warning(
            "preflight: blocked %d card(s), modified %d card(s) for safety",
            blocked_count, modified_count,
        )
        # Stash counts in session_state so the verdict logic can read them.
        try:
            run_context.session_state.setdefault("_preflight_summary", {}).update({
                "blocked": blocked_count,
                "modified": modified_count,
            })
        except Exception:
            pass
