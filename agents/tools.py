import json
import contextvars
import logging
import time
import psycopg2
from psycopg2.extras import RealDictCursor

from services.postgres_connection import assert_valid_postgres_dsn, test_postgres_connection

logger = logging.getLogger(__name__)


def _to_float(value, default: float = 0.0) -> float:
    try:
        if value is None:
            return default
        return float(value)
    except Exception:
        return default


def _to_int(value, default: int = 0) -> int:
    try:
        if value is None:
            return default
        return int(value)
    except Exception:
        return default


def _extract_confidence_overall(finding: dict) -> float:
    """Extract confidence_overall from multiple compatible finding shapes."""
    if not isinstance(finding, dict):
        return 0.0

    if isinstance(finding.get("confidence_overall"), (int, float)):
        return max(0.0, min(1.0, float(finding["confidence_overall"])))

    confidence_obj = finding.get("confidence")
    if isinstance(confidence_obj, dict):
        value = confidence_obj.get("confidence_overall")
        if isinstance(value, (int, float)):
            return max(0.0, min(1.0, float(value)))

    if isinstance(confidence_obj, (int, float)):
        return max(0.0, min(1.0, float(confidence_obj)))

    severity_defaults = {
        "critical": 0.9,
        "high": 0.8,
        "medium": 0.65,
        "low": 0.55,
    }
    severity = str(finding.get("severity") or "").strip().lower()
    return severity_defaults.get(severity, 0.6)


def _extract_evidence_meta(finding: dict) -> tuple[int, bool]:
    """
    Return evidence row_count and whether evidence.query is present.
    Backward-compatible defaults keep existing reports usable.
    """
    if not isinstance(finding, dict):
        return 0, False

    evidence = finding.get("evidence")
    if isinstance(evidence, dict):
        row_count = _to_int(evidence.get("row_count"), 0)
        query_present = bool(str(evidence.get("query") or "").strip())
        return max(row_count, 0), query_present

    # Backward compatibility: many current findings do not carry evidence object yet.
    return 1, False


def _parse_rows_payload(payload: str) -> list[dict]:
    """Parse tool JSON output into rows list. Returns [] on parse failure."""
    text = str(payload or "").strip()
    if not text or text.startswith("Error:") or text.startswith("Database Error:"):
        return []
    try:
        parsed = json.loads(text)
        if isinstance(parsed, list):
            return [row for row in parsed if isinstance(row, dict)]
    except Exception:
        return []
    return []


class PostgresTool:
    """
    Manages a PostgreSQL connection for the agent session.
    A fresh instance is bound per execution context (thread/task) to avoid
    connection-string leakage across concurrent jobs.
    """

    def __init__(self):
        self.connection_string: str | None = None

    def connect(self, connection_string: str) -> None:
        # Never let the agents start unless the DB is actually reachable.
        test_postgres_connection(connection_string, timeout_s=3)
        self.connection_string = connection_string

    def _run_query(self, query: str, limit: int = 100) -> str:
        """Internal executor – validates, limits, and runs the SQL."""
        if not self.connection_string:
            return "Error: Database connection not set. Call connect_to_database first."

        # Fast fail if the DSN is malformed (don’t leak credentials).
        try:
            assert_valid_postgres_dsn(self.connection_string)
        except Exception:
            return "Error: Invalid database connection string. Call connect_to_database with a valid DSN."

        upper = query.upper()
        banned = ["DROP", "DELETE", "UPDATE", "INSERT", "ALTER", "TRUNCATE", "GRANT", "REVOKE"]
        if any(kw in upper for kw in banned):
            return "Error: Query blocked. Only SELECT / read-only queries are allowed."

        # Enforce a row limit to protect worker memory
        if "LIMIT " not in upper:
            query = f"SELECT * FROM ({query}) AS _agno_subquery LIMIT {limit};"

        try:
            with psycopg2.connect(self.connection_string) as conn:
                with conn.cursor(cursor_factory=RealDictCursor) as cur:
                    cur.execute(query)
                    rows = cur.fetchall()

            if not rows:
                return "Query executed successfully – no rows returned."

            out = json.dumps(rows, default=str)
            if len(out) > 20_000:
                return out[:20_000] + "\n... [Output truncated for length]"
            return out

        except Exception as exc:
            logger.error("Postgres tool error for query '%s': %s", query, exc)
            return f"Database Error: {exc}"


# ---------------------------------------------------------------------------
# Context-local tool state + backward-compatible proxy.
# ---------------------------------------------------------------------------
_postgres_tool_ctx: contextvars.ContextVar[PostgresTool | None] = contextvars.ContextVar(
    "postgres_tool_ctx",
    default=None,
)


def _get_context_postgres_tool() -> PostgresTool:
    tool = _postgres_tool_ctx.get()
    if tool is None:
        tool = PostgresTool()
        _postgres_tool_ctx.set(tool)
    return tool


class _PostgresToolProxy:
    """
    Compatibility proxy that preserves `postgres_tool.connect(...)` and
    `postgres_tool._run_query(...)` call sites while isolating state per context.
    """

    def connect(self, connection_string: str) -> None:
        _get_context_postgres_tool().connect(connection_string)

    def _run_query(self, query: str, limit: int = 100) -> str:
        return _get_context_postgres_tool()._run_query(query, limit=limit)

    @property
    def connection_string(self) -> str | None:
        return _get_context_postgres_tool().connection_string


postgres_tool = _PostgresToolProxy()


# ---------------------------------------------------------------------------
# Agno-compatible tool functions
#
# Agno auto-generates the tool schema from type hints + docstrings.
# Both functions must be passed in the agent's `tools=[...]` list.
# ---------------------------------------------------------------------------

def connect_to_database(connection_string: str) -> str:
    """Connect to a PostgreSQL database before running any queries.

    Call this tool FIRST whenever the user provides a connection string.
    You cannot run execute_pg_query until a connection has been established.

    Args:
        connection_string: A full PostgreSQL DSN, e.g.
            'postgresql://user:password@host:port/database'

    Returns:
        A confirmation message showing the host/database (credentials hidden).
    """
    postgres_tool.connect(connection_string)
    # Only show the host/db part — never log credentials
    host_db = connection_string.split("@")[-1] if "@" in connection_string else connection_string
    return f"Connected successfully to: {host_db}"


def execute_pg_query(query: str) -> str:
    """Execute a read-only SQL SELECT query on the connected PostgreSQL database.

    IMPORTANT: You must call connect_to_database first if no connection is active.

    Use this tool to inspect schema or validate data:
      - Query information_schema for table/column metadata.
      - Run GROUP BY / HAVING / JOIN queries to find aggregate violations.
      - Run targeted WHERE queries to isolate row-level rule failures.

    Rules:
      - ONLY SELECT queries are allowed — destructive SQL is blocked.
      - Never select all rows without a WHERE or LIMIT clause.
      - Escape regex backslashes: use '\\\\d', NOT '\\d'.

    Args:
        query: The exact SQL SELECT statement to execute.

    Returns:
        A JSON string of query results, or an error message.
    """
    return postgres_tool._run_query(query)


def _build_introspection_queries(schema: str) -> dict[str, str]:
    return {
        "tables": (
            "SELECT table_name FROM information_schema.tables "
            f"WHERE table_schema = '{schema}' AND table_type = 'BASE TABLE' ORDER BY table_name"
        ),
        "columns": (
            "SELECT table_name, column_name, data_type, is_nullable "
            "FROM information_schema.columns "
            f"WHERE table_schema = '{schema}' ORDER BY table_name, ordinal_position"
        ),
        "constraints": (
            "SELECT tc.table_name, tc.constraint_type, tc.constraint_name "
            "FROM information_schema.table_constraints tc "
            f"WHERE tc.table_schema = '{schema}' ORDER BY tc.table_name, tc.constraint_type"
        ),
        "indexes": (
            "SELECT tablename AS table_name, indexname, indexdef "
            "FROM pg_catalog.pg_indexes "
            f"WHERE schemaname = '{schema}' ORDER BY tablename, indexname"
        ),
        "estimates": (
            "SELECT relname AS table_name, n_live_tup AS estimated_rows "
            "FROM pg_catalog.pg_stat_user_tables "
            f"WHERE schemaname = '{schema}'"
        ),
    }


def _fetch_rows(query: str, *, enabled: bool = True) -> list[dict]:
    if not enabled:
        return []
    return _parse_rows_payload(execute_pg_query(query))


def _normalize_tables(rows: list[dict], estimates: list[dict]) -> list[dict]:
    estimates_map = {str(r.get("table_name")): _to_int(r.get("estimated_rows"), 0) for r in estimates}
    normalized = []
    for row in rows:
        table_name = str(row.get("table_name") or "").strip()
        if not table_name:
            continue
        normalized.append({"table": table_name, "estimated_rows": estimates_map.get(table_name, 0)})
    return normalized


def _normalize_columns(rows: list[dict]) -> list[dict]:
    normalized = []
    for row in rows:
        table_name = str(row.get("table_name") or "").strip()
        column_name = str(row.get("column_name") or "").strip()
        if not table_name or not column_name:
            continue
        normalized.append(
            {
                "table": table_name,
                "column": column_name,
                "data_type": str(row.get("data_type") or "unknown"),
                "nullable": str(row.get("is_nullable") or "YES").upper() == "YES",
            }
        )
    return normalized


def _map_constraint_type(value: str) -> str:
    mapping = {
        "PRIMARY KEY": "PK",
        "FOREIGN KEY": "FK",
        "UNIQUE": "UNIQUE",
    }
    return mapping.get(value.upper(), "CHECK")


def _normalize_constraints(rows: list[dict]) -> list[dict]:
    normalized = []
    for row in rows:
        table_name = str(row.get("table_name") or "").strip()
        if not table_name:
            continue
        normalized.append(
            {
                "table": table_name,
                "type": _map_constraint_type(str(row.get("constraint_type") or "")),
                "name": str(row.get("constraint_name") or ""),
            }
        )
    return normalized


def _normalize_indexes(rows: list[dict]) -> list[dict]:
    normalized = []
    for row in rows:
        table_name = str(row.get("table_name") or row.get("tablename") or "").strip()
        if not table_name:
            continue
        normalized.append(
            {
                "table": table_name,
                "index": str(row.get("indexname") or ""),
                "definition": str(row.get("indexdef") or ""),
            }
        )
    return normalized


def introspect_schema_compact(
    schemas: list[str] | None = None,
    include_indexes: bool = True,
    include_constraints: bool = True,
    include_estimates: bool = True,
) -> dict:
    """
    Return compact schema metadata for guideline generation and planning.

    This helper runs a bounded set of SELECT-only queries and returns a
    normalized object with tables, columns, constraints, and indexes.
    """
    started = time.perf_counter()
    warnings: list[str] = []
    requested = schemas or ["public"]
    schema = requested[0] if requested else "public"
    queries = _build_introspection_queries(schema)

    tables = _fetch_rows(queries["tables"])
    columns = _fetch_rows(queries["columns"])
    constraints = _fetch_rows(queries["constraints"], enabled=include_constraints)
    indexes = _fetch_rows(queries["indexes"], enabled=include_indexes)
    estimates = _fetch_rows(queries["estimates"], enabled=include_estimates)

    if not tables:
        warnings.append("no_tables_found_or_query_failed")

    normalized_tables = _normalize_tables(tables, estimates)
    normalized_columns = _normalize_columns(columns)
    normalized_constraints = _normalize_constraints(constraints)
    normalized_indexes = _normalize_indexes(indexes)

    status = "ok" if normalized_tables else "partial"
    return {
        "tables": normalized_tables,
        "columns": normalized_columns,
        "constraints": normalized_constraints,
        "indexes": normalized_indexes,
        "meta": {
            "elapsed_ms": max(0, int((time.perf_counter() - started) * 1000)),
            "status": status,
            "warnings": warnings,
        },
    }


def allocate_query_budget(
    table_stats: list[dict] | None = None,
    default_limits: dict | None = None,
) -> dict:
    """
    Compute per-stage tool/query limits based on estimated database size.

    This helper is deterministic and side-effect free. It is intended for
    orchestrator-level budget assignment, not direct SQL execution.
    """
    started = time.perf_counter()
    defaults = default_limits or {
        "schema": 40,
        "column": 40,
        "row": 40,
        "cross_row": 60,
        "fix_planner": 30,
    }

    tables = table_stats or []
    total_rows = 0
    for entry in tables:
        if not isinstance(entry, dict):
            continue
        total_rows += max(_to_int(entry.get("estimated_rows"), 0), 0)

    scale = 1.0
    if total_rows >= 5_000_000:
        scale = 1.4
    elif total_rows >= 1_000_000:
        scale = 1.25
    elif total_rows >= 250_000:
        scale = 1.1
    elif total_rows > 0:
        scale = 0.95

    limits = {
        stage: max(10, int(round(_to_int(value, 20) * scale)))
        for stage, value in defaults.items()
    }

    return {
        "limits": limits,
        "meta": {
            "elapsed_ms": max(0, int((time.perf_counter() - started) * 1000)),
            "status": "ok",
            "warnings": [],
        },
    }


def enforce_quality_gate(
    findings: list[dict],
    min_confidence: float = 0.75,
    min_evidence_rows: int = 0,
    require_evidence_query: bool = False,
) -> dict:
    """
    Filter findings by confidence/evidence quality before passing downstream.

    Returns accepted findings plus a blocked list with reasons.
    """
    started = time.perf_counter()
    accepted: list[dict] = []
    blocked: list[dict] = []
    warnings: list[str] = []

    for idx, finding in enumerate(findings or []):
        keep, blocked_item = _gate_finding(
            finding=finding,
            idx=idx,
            min_confidence=min_confidence,
            min_evidence_rows=min_evidence_rows,
            require_evidence_query=require_evidence_query,
        )
        if keep:
            accepted.append(finding)
        elif blocked_item:
            blocked.append(blocked_item)

    if not accepted and findings:
        warnings.append("quality_gate_blocked_all_findings")

    status = "ok" if not blocked else "partial"
    return {
        "accepted_findings": accepted,
        "blocked": blocked,
        "meta": {
            "elapsed_ms": max(0, int((time.perf_counter() - started) * 1000)),
            "status": status,
            "warnings": warnings,
        },
    }


def _gate_finding(
    *,
    finding: dict,
    idx: int,
    min_confidence: float,
    min_evidence_rows: int,
    require_evidence_query: bool,
) -> tuple[bool, dict | None]:
    if not isinstance(finding, dict):
        return False, {"id": f"idx_{idx}", "reason": "invalid_finding_shape"}

    fid = str(finding.get("id") or finding.get("item_id") or f"idx_{idx}")
    confidence = _extract_confidence_overall(finding)
    row_count, has_query = _extract_evidence_meta(finding)

    reasons: list[str] = []
    if confidence < float(min_confidence):
        reasons.append(f"low_confidence:{confidence:.4f}")
    if row_count < int(min_evidence_rows):
        reasons.append(f"insufficient_evidence_rows:{row_count}")
    if require_evidence_query and not has_query:
        reasons.append("missing_evidence_query")

    if reasons:
        return False, {"id": fid, "reason": ";".join(reasons)}
    return True, None
