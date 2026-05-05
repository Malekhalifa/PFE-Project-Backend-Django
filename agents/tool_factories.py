"""
tool_factories.py - Per-agent toolkit factories for the QualityLab runtime.

The factories are resolved at run time by Agno, so they can build DB-bound
toolkits from the current session_state rather than global process state.
"""

from __future__ import annotations

import logging
import os
from typing import Any
from urllib.parse import unquote, urlparse

from services.postgres_connection import assert_valid_postgres_dsn, parse_postgres_dsn_info

logger = logging.getLogger(__name__)


def _custom_tools(*names: str) -> list[Any]:
    """Import and return named custom tool objects from tools.py lazily."""
    from agents import tools as t

    return [getattr(t, name) for name in names if hasattr(t, name)]


def _resolve_state(
    *,
    session_state: dict[str, Any] | None = None,
    run_context: Any | None = None,
) -> dict[str, Any]:
    state: dict[str, Any] = {}
    if run_context is not None and getattr(run_context, "session_state", None):
        state.update(run_context.session_state)
    if session_state:
        state.update(session_state)
    return state


def _resolve_mode(
    *,
    session_state: dict[str, Any] | None = None,
    run_context: Any | None = None,
) -> str:
    state = _resolve_state(session_state=session_state, run_context=run_context)
    return str(state.get("ql_mode") or os.environ.get("QL_MODE") or "pipeline").strip().lower()


def resolve_pg_tool_kwargs(
    *,
    session_state: dict[str, Any] | None = None,
    run_context: Any | None = None,
) -> dict[str, Any] | None:
    """
    Resolve PostgresTools constructor kwargs from the active runtime state.

    Returns None when no usable database connection is available yet.
    """
    state = _resolve_state(session_state=session_state, run_context=run_context)
    connection_string = (
        state.get("connection_string")
        or state.get("db_url")
        or os.environ.get("DATABASE_URL")
    )

    if connection_string:
        try:
            assert_valid_postgres_dsn(connection_string)
            parsed = urlparse(connection_string)
            info = parse_postgres_dsn_info(connection_string)
            return {
                "db_name": info.database or "",
                "host": info.host or "localhost",
                "port": info.port or 5432,
                "user": unquote(parsed.username or ""),
                "password": unquote(parsed.password or ""),
                "table_schema": state.get("table_schema") or os.environ.get("DB_SCHEMA", "public"),
            }
        except Exception as exc:
            logger.warning("Could not resolve PostgresTools config from DSN: %s", exc)
            return None

    host = os.environ.get("DB_HOST")
    db_name = os.environ.get("DB_NAME") or os.environ.get("POSTGRES_DB")
    user = os.environ.get("DB_USER") or os.environ.get("POSTGRES_USER")
    password = os.environ.get("DB_PASSWORD") or os.environ.get("POSTGRES_PASSWORD")
    port = int(os.environ.get("DB_PORT", "5432"))

    if host and db_name and user is not None and password is not None:
        return {
            "db_name": db_name,
            "host": host,
            "port": port,
            "user": user,
            "password": password,
            "table_schema": state.get("table_schema") or os.environ.get("DB_SCHEMA", "public"),
        }

    return None


def build_postgres_tools(
    *,
    include: list[str] | None = None,
    cache: bool = True,
    session_state: dict[str, Any] | None = None,
    run_context: Any | None = None,
):
    """
    Build a PostgresTools toolkit from the current run state.

    The import is intentionally lazy so the module remains importable even when
    `psycopg-binary` has not been installed yet.
    """
    kwargs = resolve_pg_tool_kwargs(session_state=session_state, run_context=run_context)
    if kwargs is None:
        return None

    from agno.tools.postgres import PostgresTools

    toolkit_kwargs = {
        **kwargs,
        "cache_results": cache,
    }
    if include is not None:
        toolkit_kwargs["include_tools"] = include

    return PostgresTools(**toolkit_kwargs)


def profiler_tools(
    *,
    session_state: dict[str, Any] | None = None,
    run_context: Any | None = None,
) -> list[Any]:
    mode = _resolve_mode(session_state=session_state, run_context=run_context)
    include = None if mode == "playground" else ["summarize_table", "run_query"]
    pg = build_postgres_tools(include=include, session_state=session_state, run_context=run_context)
    tools: list[Any] = [pg] if pg is not None else []
    tools.extend(_custom_tools("detect_outliers", "detect_patterns"))
    return tools


def schema_tools(
    *,
    session_state: dict[str, Any] | None = None,
    run_context: Any | None = None,
) -> list[Any]:
    mode = _resolve_mode(session_state=session_state, run_context=run_context)
    include = None if mode == "playground" else ["show_tables", "describe_table"]
    pg = build_postgres_tools(include=include, session_state=session_state, run_context=run_context)
    tools: list[Any] = [pg] if pg is not None else []
    # describe_constraints gives the schema agent visibility into PKs, UNIQUEs, and
    # FKs that are invisible to describe_table() (which only returns column/type/nullable).
    tools.extend(_custom_tools("describe_constraints"))
    return tools


def statistical_tools(
    *,
    session_state: dict[str, Any] | None = None,
    run_context: Any | None = None,
) -> list[Any]:
    mode = _resolve_mode(session_state=session_state, run_context=run_context)
    include = None if mode == "playground" else ["describe_table", "summarize_table", "run_query"]
    pg = build_postgres_tools(include=include, session_state=session_state, run_context=run_context)
    tools: list[Any] = [pg] if pg is not None else []
    tools.extend(_custom_tools("detect_outliers", "detect_patterns", "check_categorical_drift"))
    return tools


def structural_tools(
    *,
    session_state: dict[str, Any] | None = None,
    run_context: Any | None = None,
) -> list[Any]:
    mode = _resolve_mode(session_state=session_state, run_context=run_context)
    include = None if mode == "playground" else ["describe_table", "run_query"]
    pg = build_postgres_tools(include=include, session_state=session_state, run_context=run_context)
    tools: list[Any] = [pg] if pg is not None else []
    tools.extend(_custom_tools("find_duplicate_rows"))
    return tools


def relational_tools(
    *,
    session_state: dict[str, Any] | None = None,
    run_context: Any | None = None,
) -> list[Any]:
    mode = _resolve_mode(session_state=session_state, run_context=run_context)
    include = None if mode == "playground" else ["describe_table", "run_query"]
    pg = build_postgres_tools(include=include, session_state=session_state, run_context=run_context)
    tools: list[Any] = [pg] if pg is not None else []
    # describe_constraints is critical: without it the agent cannot see existing
    # UNIQUE/composite constraints and will falsely flag columns that already have
    # composite uniqueness enforced (e.g. UNIQUE (groupe_id, campaign_type)).
    # It also exposes the actual FK target table for orphan-reference checks.
    tools.extend(_custom_tools("describe_constraints", "analyze_relationship", "detect_fd", "diff_tables"))
    return tools


def fixplanner_tools(
    *,
    session_state: dict[str, Any] | None = None,
    run_context: Any | None = None,
) -> list[Any]:
    mode = _resolve_mode(session_state=session_state, run_context=run_context)
    # inspect_query is provided by our custom tool (tools.py) which handles DDL
    # gracefully. Exclude it from the agno PostgresTools include list to avoid the
    # agno version blindly prepending EXPLAIN to DDL statements.
    include = None if mode == "playground" else ["run_query"]
    pg = build_postgres_tools(include=include, session_state=session_state, run_context=run_context)
    tools: list[Any] = [pg] if pg is not None else []
    tools.extend(_custom_tools("inspect_query", "suggest_indexes", "check_bloat_and_dead_tuples"))
    return tools
