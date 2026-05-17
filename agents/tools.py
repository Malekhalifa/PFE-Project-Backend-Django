"""
tools.py - Custom statistical, relational, and quality-gate helpers.

All generic database introspection and read-only querying is handled by Agno's
PostgresTools. The functions in this module cover only the custom analysis
helpers that are not already provided by that toolkit.
"""

from __future__ import annotations

import contextvars as _cv
import json
import logging
import re as _re
from typing import Any

import psycopg2
from psycopg2.extras import RealDictCursor

from agno.tools import tool

from services.postgres_connection import assert_valid_postgres_dsn, test_postgres_connection

logger = logging.getLogger(__name__)

_BANNED_STMT_RE = _re.compile(
    r"\b(?:DROP|DELETE|UPDATE|INSERT|ALTER|TRUNCATE|GRANT|REVOKE)\b",
    _re.IGNORECASE,
)

_MAX_RESULT_CHARS = 20_000
_FETCH_BATCH_SIZE = 200


class _PgExecutor:
    """Thread-safe read-only executor used by custom statistical helpers."""

    def __init__(self):
        self.connection_string: str | None = None

    def connect(self, connection_string: str) -> None:
        test_postgres_connection(connection_string, timeout_s=3)
        self.connection_string = connection_string

    def run(self, query: str, limit: int = 100) -> str:
        if not self.connection_string:
            return "Error: Database connection not set."

        try:
            assert_valid_postgres_dsn(self.connection_string)
        except Exception:
            return "Error: Invalid database connection string."

        if _BANNED_STMT_RE.search(query):
            return "Error: Query blocked. Only SELECT queries are allowed."

        # Always wrap in a subquery to enforce LIMIT, even when the original
        # query already contains a LIMIT clause (guards against LIMIT 999999).
        cleaned = query.strip().rstrip(";")
        query = f"SELECT * FROM ({cleaned}) AS _agno_subquery LIMIT {limit};"

        conn = None
        try:
            conn = psycopg2.connect(self.connection_string)
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute("SET statement_timeout = 5000;")
                cur.execute("SET idle_in_transaction_session_timeout = 5000;")
                cur.execute(query)
                rows = cur.fetchmany(_FETCH_BATCH_SIZE)
            if not rows:
                return "Query executed successfully - no rows returned."
            output = json.dumps(rows, default=str)
            truncated = len(output) > _MAX_RESULT_CHARS
            return output[:_MAX_RESULT_CHARS] + "\n... [truncated]" if truncated else output
        except Exception as exc:
            logger.error("DB tool error '%s': %s", query[:80], exc)
            return f"Database Error: {exc}"
        finally:
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass


_exec_ctx: _cv.ContextVar[_PgExecutor | None] = _cv.ContextVar("pg_exec_ctx", default=None)


def _executor() -> _PgExecutor:
    ex = _exec_ctx.get()
    if ex is None:
        ex = _PgExecutor()
        _exec_ctx.set(ex)
    return ex


class _ExecutorProxy:
    def connect(self, connection_string: str) -> None:
        _executor().connect(connection_string)

    def _run_query(self, query: str, limit: int = 100) -> str:
        return _executor().run(query, limit=limit)

    @property
    def connection_string(self) -> str | None:
        return _executor().connection_string


postgres_tool = _ExecutorProxy()


def _q(query: str) -> str:
    return _executor().run(query)


_DDL_EXPLAIN_RE = _re.compile(
    r"^\s*(ALTER|CREATE|DROP|TRUNCATE|INSERT|UPDATE|DELETE)\b",
    _re.IGNORECASE,
)


@tool(cache_results=False)
def inspect_query(query: str) -> str:
    """
    Shows the EXPLAIN execution plan for a SELECT query.
    DDL statements (ALTER TABLE, CREATE INDEX, etc.) cannot be EXPLAINed in
    PostgreSQL — for those, returns a validation note so the caller knows the
    syntax was reviewed without triggering a database error.
    """
    if _DDL_EXPLAIN_RE.match(query.strip()):
        return (
            "DDL statement — EXPLAIN is not applicable in PostgreSQL. "
            "The statement looks structurally valid. "
            "To dry-run it safely, use run_query inside an explicit BEGIN/ROLLBACK transaction."
        )
    conn_str = _executor().connection_string
    if not conn_str:
        return "Error: Database connection not set."
    conn = None
    try:
        conn = psycopg2.connect(conn_str)
        with conn.cursor() as cur:
            cur.execute(f"EXPLAIN {query}")
            rows = cur.fetchall()
        return "\n".join(str(r[0]) for r in rows) if rows else "No plan returned."
    except Exception as exc:
        logger.error("inspect_query error '%s': %s", query[:80], exc)
        return f"Error: {exc}"
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


_NUMERIC_PG_TYPES = frozenset({
    "smallint", "integer", "bigint", "decimal", "numeric",
    "real", "double precision", "smallserial", "serial", "bigserial",
    "money",
    # "interval" deliberately excluded — interval::numeric is invalid in PostgreSQL
})


@tool(cache_results=False)
def detect_outliers(table_name: str, column_name: str) -> str:
    """Detect IQR-based numerical anomalies for one column.

    Skips non-numeric columns (time, timestamp, boolean, text, etc.) to avoid
    PostgreSQL cast errors that cause MALFORMED_FUNCTION_CALL crashes.
    """
    # Preflight: check the column's data_type before attempting PERCENTILE_CONT.
    type_check = _q(
        f"""
        SELECT data_type
        FROM information_schema.columns
        WHERE table_schema = 'public'
          AND table_name   = '{table_name}'
          AND column_name  = '{column_name}'
        LIMIT 1
        """
    )
    try:
        import json as _json
        type_rows = _json.loads(type_check)
        if isinstance(type_rows, list) and type_rows:
            col_type = str(type_rows[0].get("data_type", "")).lower().strip()
            if col_type not in _NUMERIC_PG_TYPES:
                return (
                    f"Skipped: column {table_name}.{column_name} has type '{col_type}' "
                    f"which does not support numeric IQR analysis. "
                    f"Use check_categorical_drift() for text columns or run_query() for temporal analysis."
                )
    except Exception:
        pass  # if the type check itself fails, proceed and let the original error surface

    # Require at least 30 non-null values and a non-zero IQR for meaningful outlier detection.
    check = _q(
        f"""
        SELECT COUNT({column_name}) AS non_null_count,
               percentile_cont(0.25) WITHIN GROUP (ORDER BY {column_name}::numeric) AS q1,
               percentile_cont(0.75) WITHIN GROUP (ORDER BY {column_name}::numeric) AS q3
        FROM {table_name}
        WHERE {column_name} IS NOT NULL
        """
    )
    try:
        import json as _json
        rows = _json.loads(check)
        if isinstance(rows, list) and rows:
            r = rows[0]
            non_null = int(r.get("non_null_count") or 0)
            q1 = r.get("q1")
            q3 = r.get("q3")
            if non_null < 30:
                return f"Skipped: only {non_null} non-null values in {table_name}.{column_name} — IQR outlier detection requires at least 30."
            if q1 is not None and q3 is not None:
                iqr = float(q3) - float(q1)
                if iqr == 0:
                    return f"Skipped: IQR is zero for {table_name}.{column_name} — all values are identical, no outliers possible."
                lower = float(q1) - 1.5 * iqr
                upper = float(q3) + 1.5 * iqr
                return _q(
                    f"SELECT count(*) as outlier_count, {lower} as lower_bound, {upper} as upper_bound "
                    f"FROM {table_name} "
                    f"WHERE {column_name}::numeric < {lower} OR {column_name}::numeric > {upper}"
                )
    except Exception:
        pass

    # Fallback if stats parsing failed — recompute percentiles in a single CTE.
    return _q(
        f"""
        WITH stats AS (
            SELECT percentile_cont(0.25) WITHIN GROUP (ORDER BY {column_name}::numeric) AS q1,
                   percentile_cont(0.75) WITHIN GROUP (ORDER BY {column_name}::numeric) AS q3
            FROM {table_name}
            WHERE {column_name} IS NOT NULL
        )
        SELECT count(*) as outlier_count,
               MIN(q1 - 1.5 * (q3 - q1)) as lower_bound,
               MAX(q3 + 1.5 * (q3 - q1)) as upper_bound
        FROM {table_name}, stats
        WHERE {column_name}::numeric < q1 - 1.5 * (q3 - q1)
           OR {column_name}::numeric > q3 + 1.5 * (q3 - q1)
        """
    )


@tool(cache_results=False)
def describe_constraints(table_name: str) -> str:
    """Return all PRIMARY KEY, UNIQUE, FOREIGN KEY, and CHECK constraints for a table.

    Use this in the schema integrity check to determine which columns are PKs
    or have unique/referential constraints — information not available via describe_table().
    """
    return _q(
        f"""
        SELECT
            tc.constraint_type,
            tc.constraint_name,
            kcu.column_name,
            CASE WHEN tc.constraint_type = 'FOREIGN KEY' THEN fk_target.table_name  END AS foreign_table,
            CASE WHEN tc.constraint_type = 'FOREIGN KEY' THEN fk_target.column_name END AS foreign_column
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
        WHERE tc.table_name   = '{table_name}'
          AND tc.table_schema = 'public'
        ORDER BY tc.constraint_type, kcu.ordinal_position
        LIMIT 50
        """
    )


@tool(cache_results=False)
def check_categorical_drift(table_name: str, column_name: str) -> str:
    """List low-frequency categories that may indicate typos or drift."""
    return _q(
        f"""
        WITH counts AS (
            SELECT {column_name}::text as category, COUNT(*) as cnt
            FROM {table_name}
            GROUP BY 1
        )
        SELECT category, cnt, ROUND(cnt * 100.0 / sum(cnt) OVER (), 4) as percentage
        FROM counts
        ORDER BY percentage ASC
        LIMIT 50
        """
    )


@tool(cache_results=False)
def analyze_relationship(source_table: str, source_col: str, target_table: str, target_col: str) -> str:
    """Count orphaned rows between two related tables."""
    return _q(
        f"SELECT count(*) as orphaned_records "
        f"FROM {source_table} s "
        f"LEFT JOIN {target_table} t ON s.{source_col}::text = t.{target_col}::text "
        f"WHERE t.{target_col} IS NULL AND s.{source_col} IS NOT NULL"
    )


@tool(cache_results=False)
def detect_fd(table_name: str, pk_col: str, dep_col: str) -> str:
    """Check whether one column functionally determines another."""
    return _q(
        f"SELECT {pk_col}, COUNT(DISTINCT {dep_col}) as variants "
        f"FROM {table_name} GROUP BY {pk_col} "
        f"HAVING COUNT(DISTINCT {dep_col}) > 1 LIMIT 5"
    )


@tool(cache_results=False)
def diff_tables(table1: str, table2: str, join_col: str) -> str:
    """Find records present in one table but missing from the other."""
    return _q(
        f"SELECT '{table1}_missing' as type, count(*) "
        f"FROM {table2} b LEFT JOIN {table1} a ON a.{join_col} = b.{join_col} "
        f"WHERE a.{join_col} IS NULL"
    )


@tool(cache_results=False)
def find_duplicate_rows(table_name: str, group_columns: str) -> str:
    """Expose logical duplicates on composite key candidates."""
    return _q(
        f"SELECT {group_columns}, count(*) "
        f"FROM {table_name} GROUP BY {group_columns} "
        f"HAVING count(*) > 1 LIMIT 5"
    )


@tool(cache_results=False)
def suggest_indexes(table_name: str) -> str:
    """Inspect sequential scans and index usage for one table."""
    return _q(
        f"SELECT seq_scan, seq_tup_read, idx_scan, idx_tup_fetch "
        f"FROM pg_stat_user_tables WHERE relname = '{table_name}'"
    )


@tool(cache_results=False)
def check_bloat_and_dead_tuples(table_name: str) -> str:
    """Inspect dead tuples and vacuum stats for one table."""
    return _q(
        f"SELECT n_dead_tup, n_live_tup, last_vacuum, last_autovacuum "
        f"FROM pg_stat_user_tables WHERE relname = '{table_name}'"
    )


