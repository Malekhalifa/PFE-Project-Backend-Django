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

    def execute(self, *, queries: list[str], connection_string: str, admin_dsn: str | None = None) -> dict:
        """
        Execute all queries in order within one transaction and return an execution log.

        The payload is designed for direct frontend rendering.

        admin_dsn: privileged DSN used only for CREATE/DROP DATABASE on the target server.
                   Must belong to a role with CREATEDB.  Defaults to the job DSN's own user
                   (which requires that user to hold CREATEDB on the target server).
        """
        self._validate_queries(queries)

        executed_at = utc_now_iso()
        started_at = time.perf_counter()
        parsed = urlparse(connection_string)
        original_db = (parsed.path or "").lstrip("/")
        sandbox_db = f"{original_db}_clean"

        # 1. Admin setup: disconnect users and duplicate DB.
        # Use the supplied admin_dsn (app admin credentials) when available so that
        # the job DSN's user does not need CREATEDB on the target server.
        if admin_dsn:
            admin_parsed = urlparse(admin_dsn)
            _admin_dsn = urlunparse(admin_parsed._replace(path="/postgres"))
        else:
            _admin_dsn = urlunparse(parsed._replace(path="/postgres"))
        sandbox_connection_string = urlunparse(parsed._replace(path=f"/{sandbox_db}"))
        
        admin_conn = None
        try:
            admin_conn = psycopg2.connect(_admin_dsn)
            admin_conn.autocommit = True
            with admin_conn.cursor() as admin_cursor:
                # Always free connections so we can use/create the sandbox cleanly.
                admin_cursor.execute(
                    "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                    "WHERE datname = %s AND pid <> pg_backend_pid();",
                    [original_db],
                )
                admin_cursor.execute(
                    "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                    "WHERE datname = %s AND pid <> pg_backend_pid();",
                    [sandbox_db],
                )

                # Reuse the sandbox if it already exists so that successive fix
                # executions accumulate — each fix is applied on top of the
                # previous ones rather than starting from a clean slate every time.
                # This allows the re-run analysis to see all fixes in effect.
                admin_cursor.execute(
                    "SELECT 1 FROM pg_catalog.pg_database WHERE datname = %s",
                    [sandbox_db],
                )
                sandbox_exists = admin_cursor.fetchone() is not None

                if not sandbox_exists:
                    admin_cursor.execute(
                        f'CREATE DATABASE "{sandbox_db}" WITH TEMPLATE "{original_db}";'
                    )

                    admin_cursor.execute(
                        "SELECT pg_catalog.pg_get_userbyid(datdba) "
                        "FROM pg_catalog.pg_database WHERE datname = %s;",
                        [original_db],
                    )
                    row = admin_cursor.fetchone()
                    original_owner = row[0] if row else None

                    if original_owner:
                        try:
                            admin_cursor.execute(
                                f'ALTER DATABASE "{sandbox_db}" OWNER TO "{original_owner}";'
                            )
                        except Exception:
                            admin_cursor.execute(
                                f'GRANT ALL PRIVILEGES ON DATABASE "{sandbox_db}" '
                                f'TO "{original_owner}";'
                            )

                    job_user = parsed.username
                    if job_user and job_user != original_owner:
                        admin_cursor.execute(
                            f'GRANT ALL PRIVILEGES ON DATABASE "{sandbox_db}" '
                            f'TO "{job_user}";'
                        )
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

    def execute_streaming(
        self, *, queries: list[str], connection_string: str, admin_dsn: str | None = None
    ):
        """
        Generator variant of execute() that yields per-query progress dicts.

        Yields:
          {"event": "setup",    "status": "ok"|"error", ...}
          {"event": "query",    "index": i, "total": n, "status": "success"|"failure", ...}
          {"event": "complete", "status": "success"|"failure", "transaction": ..., ...}

        All queries still run inside a single transaction; the stream is just
        progress reporting, not per-query commit.
        """
        self._validate_queries(queries)

        executed_at = utc_now_iso()
        started_at = time.perf_counter()
        parsed = urlparse(connection_string)
        original_db = (parsed.path or "").lstrip("/")
        sandbox_db = f"{original_db}_clean"

        # ── Admin setup (sandbox creation) ───────────────────────────────
        if admin_dsn:
            admin_parsed = urlparse(admin_dsn)
            _admin_dsn = urlunparse(admin_parsed._replace(path="/postgres"))
        else:
            _admin_dsn = urlunparse(parsed._replace(path="/postgres"))
        sandbox_connection_string = urlunparse(parsed._replace(path=f"/{sandbox_db}"))

        admin_conn = None
        try:
            admin_conn = psycopg2.connect(_admin_dsn)
            admin_conn.autocommit = True
            with admin_conn.cursor() as admin_cursor:
                admin_cursor.execute(
                    "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                    "WHERE datname = %s AND pid <> pg_backend_pid();",
                    [original_db],
                )
                admin_cursor.execute(
                    "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                    "WHERE datname = %s AND pid <> pg_backend_pid();",
                    [sandbox_db],
                )
                admin_cursor.execute(
                    "SELECT 1 FROM pg_catalog.pg_database WHERE datname = %s",
                    [sandbox_db],
                )
                sandbox_exists = admin_cursor.fetchone() is not None

                if not sandbox_exists:
                    admin_cursor.execute(
                        f'CREATE DATABASE "{sandbox_db}" WITH TEMPLATE "{original_db}";'
                    )

                    admin_cursor.execute(
                        "SELECT pg_catalog.pg_get_userbyid(datdba) "
                        "FROM pg_catalog.pg_database WHERE datname = %s;",
                        [original_db],
                    )
                    row = admin_cursor.fetchone()
                    original_owner = row[0] if row else None

                    if original_owner:
                        try:
                            admin_cursor.execute(
                                f'ALTER DATABASE "{sandbox_db}" OWNER TO "{original_owner}";'
                            )
                        except Exception:
                            admin_cursor.execute(
                                f'GRANT ALL PRIVILEGES ON DATABASE "{sandbox_db}" '
                                f'TO "{original_owner}";'
                            )

                    job_user = parsed.username
                    if job_user and job_user != original_owner:
                        admin_cursor.execute(
                            f'GRANT ALL PRIVILEGES ON DATABASE "{sandbox_db}" '
                            f'TO "{job_user}";'
                        )
            yield {"event": "setup", "status": "ok", "sandbox_db": sandbox_db}
        except Exception as exc:
            yield {
                "event": "setup",
                "status": "error",
                "error": self._build_db_error(exc),
            }
            yield {
                "event": "complete",
                "status": "failure",
                "transaction": "aborted",
                "duration_ms": self._elapsed_ms(started_at),
                "executed_at": executed_at,
            }
            return
        finally:
            self._safe_close(admin_conn)

        database_id = derive_database_id(sandbox_connection_string)
        connection = None
        cursor = None
        query_logs: list[dict] = []
        total_rows_affected = 0
        failure_exc = None

        try:
            connection = psycopg2.connect(sandbox_connection_string)
            connection.autocommit = False
            cursor = connection.cursor()
            cursor.execute("BEGIN;")

            for idx, query in enumerate(queries, start=1):
                q_start = time.perf_counter()
                try:
                    cursor.execute(query)
                    rows_affected = cursor.rowcount if cursor.rowcount >= 0 else 0
                    total_rows_affected += rows_affected
                    log = {
                        "index": idx,
                        "query": query,
                        "status": "success",
                        "duration_ms": self._elapsed_ms(q_start),
                        "rows_affected": rows_affected,
                        "error": None,
                    }
                    query_logs.append(log)
                    yield {"event": "query", "total": len(queries), **log}
                except Exception as exc:
                    failure_exc = exc
                    log = {
                        "index": idx,
                        "query": query,
                        "status": "failure",
                        "duration_ms": self._elapsed_ms(q_start),
                        "rows_affected": 0,
                        "error": self._build_db_error(exc),
                    }
                    query_logs.append(log)
                    yield {"event": "query", "total": len(queries), **log}
                    break

            if failure_exc is not None:
                self._safe_rollback(connection)
                yield {
                    "event": "complete",
                    "status": "failure",
                    "transaction": "rolled_back",
                    "database_id": database_id,
                    "executed_at": executed_at,
                    "duration_ms": self._elapsed_ms(started_at),
                    "rows_affected": total_rows_affected,
                    "error": self._build_db_error(failure_exc),
                }
            else:
                connection.commit()
                yield {
                    "event": "complete",
                    "status": "success",
                    "transaction": "committed",
                    "database_id": database_id,
                    "executed_at": executed_at,
                    "duration_ms": self._elapsed_ms(started_at),
                    "rows_affected": total_rows_affected,
                    "error": None,
                }

        except Exception as exc:
            self._safe_rollback(connection)
            yield {
                "event": "complete",
                "status": "failure",
                "transaction": "rolled_back",
                "database_id": database_id,
                "executed_at": executed_at,
                "duration_ms": self._elapsed_ms(started_at),
                "rows_affected": total_rows_affected,
                "error": self._build_db_error(exc),
            }
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
