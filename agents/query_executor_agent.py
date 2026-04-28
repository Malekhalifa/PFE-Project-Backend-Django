"""
Standalone query execution agent for human-in-the-loop fix application.

This agent is intentionally isolated and stateless:
  - one invocation executes one fix card payload
  - all queries run in a single transaction
  - no retries, no query rewriting
  - connection is always closed
"""

import time

import psycopg2

from urllib.parse import urlparse, urlunparse
from datetime import datetime, timezone

from .reporting import derive_database_id

def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


class QueryExecutorAgent:
    """Executes SQL fix queries inside one PostgreSQL transaction."""

    name = "QueryExecutorAgent"

    def execute(self, *, queries: list[str], connection_string: str) -> dict:
        """
        Execute all queries in order within one transaction and return an execution log.

        The payload is designed for direct frontend rendering.
        """
        self._validate_queries(queries)

        executed_at = utc_now_iso()
        started_at = time.perf_counter()
        parsed = urlparse(connection_string)
        original_db = (parsed.path or "").lstrip("/")
        sandbox_db = f"{original_db}_clean"
        
        # 1. Admin setup: disconnect users and duplicate DB
        admin_dsn = urlunparse(parsed._replace(path="/postgres"))
        sandbox_connection_string = urlunparse(parsed._replace(path=f"/{sandbox_db}"))
        
        admin_conn = None
        try:
            admin_conn = psycopg2.connect(admin_dsn)
            admin_conn.autocommit = True
            with admin_conn.cursor() as admin_cursor:
                # Terminate any connections blocking original_db and sandbox_db
                admin_cursor.execute("SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = %s AND pid <> pg_backend_pid();", [original_db])
                admin_cursor.execute("SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = %s AND pid <> pg_backend_pid();", [sandbox_db])
                
                admin_cursor.execute(f'DROP DATABASE IF EXISTS "{sandbox_db}";')
                admin_cursor.execute(f'CREATE DATABASE "{sandbox_db}" WITH TEMPLATE "{original_db}";')
        except Exception as exc:
            return self._failure_payload(
                queries=queries,
                database_id=original_db,
                executed_at=executed_at,
                started_at=started_at,
                query_logs=[{
                    "index": 0,
                    "query": f'CREATE DATABASE "{sandbox_db}" WITH TEMPLATE "{original_db}";',
                    "status": "failure",
                    "duration_ms": self._elapsed_ms(started_at),
                    "rows_affected": 0,
                    "error": self._build_db_error(exc)
                }],
                error=self._build_db_error(exc),
            )
        finally:
            self._safe_close(admin_conn)

        database_id = derive_database_id(sandbox_connection_string)

        connection = None
        cursor = None

        query_logs: list[dict] = []

        try:
            connection = psycopg2.connect(sandbox_connection_string)
            connection.autocommit = False
            cursor = connection.cursor()

            # Start an explicit transaction boundary before executing user SQL.
            cursor.execute("BEGIN;")

            query_logs, total_rows_affected, failure_exc = self._execute_queries(cursor, queries)
            if failure_exc is not None:
                self._safe_rollback(connection)
                return self._failure_payload(
                    queries=queries,
                    database_id=database_id,
                    executed_at=executed_at,
                    started_at=started_at,
                    query_logs=query_logs,
                    error=self._build_db_error(failure_exc),
                )

            connection.commit()
            return self._success_payload(
                queries=queries,
                database_id=database_id,
                executed_at=executed_at,
                started_at=started_at,
                rows_affected=total_rows_affected,
                query_logs=query_logs,
            )

        except Exception as exc:
            self._safe_rollback(connection)
            return self._failure_payload(
                queries=queries,
                database_id=database_id,
                executed_at=executed_at,
                started_at=started_at,
                query_logs=query_logs,
                error=self._build_db_error(exc),
            )

        finally:
            self._safe_close(cursor)
            self._safe_close(connection)

    def _execute_queries(self, cursor, queries: list[str]) -> tuple[list[dict], int, Exception | None]:
        query_logs: list[dict] = []
        total_rows_affected = 0

        for idx, query in enumerate(queries, start=1):
            query_started_at = time.perf_counter()
            try:
                cursor.execute(query)
            except Exception as exc:
                query_logs.append(
                    {
                        "index": idx,
                        "query": query,
                        "status": "failure",
                        "duration_ms": self._elapsed_ms(query_started_at),
                        "rows_affected": 0,
                        "error": self._build_db_error(exc),
                    }
                )
                return query_logs, total_rows_affected, exc

            rows_affected = cursor.rowcount if cursor.rowcount >= 0 else 0
            total_rows_affected += rows_affected
            query_logs.append(
                {
                    "index": idx,
                    "query": query,
                    "status": "success",
                    "duration_ms": self._elapsed_ms(query_started_at),
                    "rows_affected": rows_affected,
                    "error": None,
                }
            )

        return query_logs, total_rows_affected, None

    def _success_payload(
        self,
        *,
        queries: list[str],
        database_id: str,
        executed_at: str,
        started_at: float,
        rows_affected: int,
        query_logs: list[dict],
    ) -> dict:
        return {
            "status": "success",
            "query": "\n\n".join(queries),
            "queries": list(queries),
            "database_id": database_id,
            "executed_at": executed_at,
            "duration_ms": self._elapsed_ms(started_at),
            "rows_affected": rows_affected,
            "transaction": "committed",
            "error": None,
            "query_logs": query_logs,
        }

    def _failure_payload(
        self,
        *,
        queries: list[str],
        database_id: str,
        executed_at: str,
        started_at: float,
        query_logs: list[dict],
        error: dict,
    ) -> dict:
        return {
            "status": "failure",
            "query": "\n\n".join(queries),
            "queries": list(queries),
            "database_id": database_id,
            "executed_at": executed_at,
            "duration_ms": self._elapsed_ms(started_at),
            "rows_affected": 0,
            "transaction": "rolled_back",
            "error": error,
            "query_logs": query_logs,
        }

    @staticmethod
    def _validate_queries(queries: list[str]) -> None:
        if not isinstance(queries, list) or not queries:
            raise ValueError("queries must be a non-empty list of SQL strings.")
        if any(not isinstance(q, str) or not q.strip() for q in queries):
            raise ValueError("Each query must be a non-empty SQL string.")

    @staticmethod
    def _elapsed_ms(started_at: float) -> int:
        return max(0, int((time.perf_counter() - started_at) * 1000))

    @staticmethod
    def _safe_rollback(connection) -> None:
        if connection is None:
            return
        try:
            connection.rollback()
        except Exception:
            pass

    @staticmethod
    def _safe_close(resource) -> None:
        if resource is None:
            return
        try:
            resource.close()
        except Exception:
            pass

    @staticmethod
    def _build_db_error(exc: Exception) -> dict:
        diag = getattr(exc, "diag", None)
        detail = getattr(diag, "message_detail", None) if diag is not None else None
        code = getattr(exc, "pgcode", None)

        return {
            "code": str(code) if code is not None else None,
            "message": str(exc),
            "detail": detail if detail else None,
        }
