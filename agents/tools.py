import json
import logging
import psycopg2
from psycopg2.extras import RealDictCursor

from services.postgres_connection import assert_valid_postgres_dsn, test_postgres_connection

logger = logging.getLogger(__name__)


class PostgresTool:
    """
    Manages a PostgreSQL connection for the agent session.
    The singleton instance is set up once per Celery task via connect(),
    or dynamically from the playground chat via the connect_to_database tool.
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
# Module-level singleton – shared within a single execution context.
# ---------------------------------------------------------------------------
postgres_tool = PostgresTool()


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
