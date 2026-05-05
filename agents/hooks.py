"""
hooks.py - Lifecycle hooks and runtime helpers for the QualityLab pipeline.
"""

from __future__ import annotations

import csv
import fnmatch
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
    """Shared tool hook for in-run deduplication and timing telemetry.

    Two-layer cache: L1 = in-process dict (zero latency for same-worker repeat
    calls); L2 = Redis (shared across parallel Celery workers for the same job).
    """
    job_id = (run_context.session_state or {}).get("job_id") or "nojob"
    seen: dict[str, Any] = run_context.session_state.setdefault("_tool_calls_seen", {})
    try:
        arg_str = json.dumps(arguments, sort_keys=True, default=str)
    except Exception:
        arg_str = str(arguments)

    local_key = f"{function_name}:{arg_str}"
    redis_key = f"ql:tool:{job_id}:{local_key}"

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

    logger.info(
        "tool=%s ms=%.1f args=%s",
        function_name,
        elapsed_ms,
        {k: v for k, v in arguments.items() if k != "password"},
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
        "raw": str(payload or ""),
    }


def prime_connection_from_prompt(run_input: RunInput, run_context: RunContext) -> None:
    """
    Detect a PostgreSQL DSN in the current prompt and store it in session_state.

    This lets AgentOS/playground members build tools from session_state without a
    separate connect tool or global environment mutation.
    """
    state = run_context.session_state
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
    run_context.session_state["_run_start"] = time.time()


def _load_excluded_table_patterns() -> list[str]:
    """Return glob patterns for tables that must be excluded from all analysis."""
    from .rules import domain_rules as load_domain_rules
    rules = load_domain_rules()
    return list(rules.get("excluded_tables", []))


def _is_excluded(table_name: str, patterns: list[str]) -> bool:
    return any(fnmatch.fnmatch(table_name, pat) for pat in patterns)


def ensure_schema_primed(run_input: RunInput, run_context: RunContext) -> None:
    """
    Populate session_state["schema"] from PostgresTools.show_tables/describe_table.

    Tables matching excluded_tables patterns in domain_rules are filtered out here
    so no agent sees or analyses them — enforcement at the data level, not prompt level.

    In workflow mode we require a database connection. In playground mode we only
    attempt priming when a DSN is already available.
    """
    state = run_context.session_state
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

    excluded_patterns = _load_excluded_table_patterns()
    all_tables = _normalize_table_list_result(pg.show_tables())
    tables = [t for t in all_tables if not _is_excluded(t, excluded_patterns)]

    if len(all_tables) != len(tables):
        skipped = sorted(set(all_tables) - set(tables))
        logger.info("ensure_schema_primed: excluded %d table(s): %s", len(skipped), skipped)

    schema: dict[str, Any] = {}
    for table_name in tables:
        try:
            schema[table_name] = _normalize_describe_table_result(
                table_name,
                pg.describe_table(table_name),
            )
        except Exception as exc:
            logger.warning("ensure_schema_primed: failed to describe %s: %s", table_name, exc)

    state["schema"] = schema
    logger.info("ensure_schema_primed: loaded %d tables", len(schema))

    connection_string = state.get("connection_string")
    if connection_string:
        from .tools import postgres_tool
        postgres_tool.connect(connection_string)


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

    schema: dict[str, Any] = run_context.session_state.get("schema") or {}
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

    Stored as a plain dict under session_state["profiler_report"] so it survives
    JSON serialization across workflow steps and is resolvable via {profiler_report}
    in agent instruction templates.
    """
    content = run_output.content
    if not isinstance(content, ProfilerReport):
        return
    try:
        run_context.session_state["profiler_report"] = content.model_dump()
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
    state = run_context.session_state
    elapsed_ms = (time.time() - state.get("_run_start", time.time())) * 1000
    tool_calls = len(state.get("_tool_calls_seen", {}))

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
                # sqlglot falls back to 'Command' for valid DDL it cannot fully parse
                # (e.g. multi-column ALTER TABLE). Check the raw text before rejecting.
                if statement_type == "Command":
                    raw_upper = sql_text.strip().upper()
                    _safe_ddl = ("ALTER TABLE", "CREATE INDEX", "CREATE TABLE", "COMMENT ON")
                    if any(raw_upper.startswith(p) for p in _safe_ddl):
                        continue
                violations.append(f"{card.target}: unexpected statement type '{statement_type}'")

    if violations:
        raise OutputCheckError(
            f"FixPlan contains unsafe SQL: {violations[:5]}",
            check_trigger=CheckTrigger.OUTPUT_NOT_ALLOWED,
        )
