"""
PostgreSQL Cleaning Engine
==========================

Executes a structured cleaning plan (produced by the Data Cleaning Planner agent)
against the user's PostgreSQL database.

Entry point
-----------
    run_cleaning_plan(connection_string, plan) -> dict

The connection_string is the user's own database DSN — NOT Django's database.
psycopg2 is used directly; Django ORM is never touched here.

All table / column identifiers are handled exclusively through
psycopg2.sql.Identifier / psycopg2.sql.SQL to prevent SQL injection.

Logging
-------
Every SQL statement is logged to the "qualitylab.cleaning" logger at DEBUG level.
Errors are logged at ERROR level.
"""

import logging
from typing import Optional

import psycopg2
import psycopg2.extras
from psycopg2 import sql

logger = logging.getLogger("qualitylab.cleaning")


# ──────────────────────────────────────────────────────────
# Action handlers — each returns a list[str] of rendered SQL
# (as plain strings for logging) plus the composed SQL objects
# for psycopg2 execution, or None when the action must be skipped.
# ──────────────────────────────────────────────────────────


def _handle_cast(
    column: str,
    params: dict,
    target_table: str,
) -> Optional[list]:
    """
    ALTER COLUMN <column> TYPE <target_type> USING <column>::<target_type>

    Returns None if requires_user_input is True (graceful skip).
    Returns None if target_type is absent (cannot safely cast).
    """
    if params.get("requires_user_input"):
        return None  # caller marks action as skipped_needs_input

    target_type = params.get("target_type")
    if not target_type:
        # No safe default — skip rather than guess
        return None

    stmt = sql.SQL(
        "ALTER TABLE {tbl} ALTER COLUMN {col} TYPE {typ} USING {col}::{typ}"
    ).format(
        tbl=sql.Identifier(target_table),
        col=sql.Identifier(column),
        typ=sql.SQL(target_type),  # target_type is a PG built-in keyword, not user data
    )
    return [stmt]


def _handle_fill_null(
    column: str,
    params: dict,
    target_table: str,
) -> list:
    """
    Fill NULL values in <column> using the specified method.

    Supported methods:
        "mode" — replace NULLs with the most frequent non-NULL value
    """
    method = params.get("method", "mode")

    if method == "mode":
        stmt = sql.SQL(
            """
            UPDATE {tbl}
            SET {col} = (
                SELECT {col}
                FROM {tbl}
                WHERE {col} IS NOT NULL
                GROUP BY {col}
                ORDER BY COUNT(*) DESC
                LIMIT 1
            )
            WHERE {col} IS NULL
            """
        ).format(
            tbl=sql.Identifier(target_table),
            col=sql.Identifier(column),
        )
        return [stmt]

    # Unknown method — log as a warning but don't crash; return empty list
    logger.warning(
        "fill_null: unknown method %r for column %r in table %r — skipping",
        method,
        column,
        target_table,
    )
    return []


def _handle_drop_duplicates(
    column: Optional[str],
    params: dict,
    target_table: str,
) -> list:
    """
    Remove duplicate rows.

    Two modes:
    1. Single column: column is not None
       DELETE rows keeping the one with MIN(ctid) per column value.

    2. Composite key: column is None, params["candidate_columns"] is a list
       DELETE using a self-join on ctid to keep one row per composite key.
    """
    tbl = sql.Identifier(target_table)

    if column is not None:
        # Single-column dedup
        stmt = sql.SQL(
            """
            DELETE FROM {tbl}
            WHERE ctid NOT IN (
                SELECT MIN(ctid)
                FROM {tbl}
                GROUP BY {col}
            )
            """
        ).format(tbl=tbl, col=sql.Identifier(column))
        return [stmt]

    candidate_columns = params.get("candidate_columns")
    if candidate_columns:
        # Composite-key dedup using a self-join
        join_conditions = sql.SQL(" AND ").join(
            sql.SQL("a.{col} = b.{col}").format(col=sql.Identifier(c))
            for c in candidate_columns
        )
        stmt = sql.SQL(
            """
            DELETE FROM {tbl} a
            USING {tbl} b
            WHERE a.ctid < b.ctid
              AND {conditions}
            """
        ).format(tbl=tbl, conditions=join_conditions)
        return [stmt]

    # Nothing to dedup on — skip
    logger.warning(
        "drop_duplicates: no column or candidate_columns specified for table %r — skipping",
        target_table,
    )
    return []


# ──────────────────────────────────────────────────────────
# Dispatch table
# ──────────────────────────────────────────────────────────

HANDLERS = {
    "cast": _handle_cast,
    "fill_null": _handle_fill_null,
    "drop_duplicates": _handle_drop_duplicates,
}


def _compile_action(action: dict, target_table: str) -> Optional[list]:
    """
    Dispatch an action dict to the correct handler.

    Returns:
        None  → action should be skipped (requires_user_input or unknown type)
        []    → action compiled to zero SQL statements (harmless no-op)
        [...]  → list of psycopg2 sql.Composed objects ready to execute
    """
    action_type = action.get("type")
    column = action.get("column")          # may be None for composite dedup
    params = action.get("params") or {}

    handler = HANDLERS.get(action_type)
    if handler is None:
        logger.warning("Unknown action type %r — skipping", action_type)
        return None

    return handler(column, params, target_table)


# ──────────────────────────────────────────────────────────
# Table-level helpers
# ──────────────────────────────────────────────────────────

def _create_target_table(cursor, source_table: str, target_table: str) -> None:
    """
    Idempotently create <target_table> as a copy of <source_table>.

    CREATE TABLE IF NOT EXISTS <target_table> (LIKE <source_table> INCLUDING ALL)
    does NOT copy data, so we follow with INSERT … SELECT *.

    We use LIKE … INCLUDING ALL to also copy constraints & indexes.
    """
    create_stmt = sql.SQL(
        "CREATE TABLE IF NOT EXISTS {tgt} (LIKE {src} INCLUDING ALL)"
    ).format(
        tgt=sql.Identifier(target_table),
        src=sql.Identifier(source_table),
    )
    logger.debug("SQL: %s", create_stmt.as_string(cursor))
    cursor.execute(create_stmt)

    # Only insert if the target is empty (idempotent re-runs)
    count_stmt = sql.SQL("SELECT COUNT(*) FROM {tgt}").format(
        tgt=sql.Identifier(target_table)
    )
    cursor.execute(count_stmt)
    row_count = cursor.fetchone()[0]

    if row_count == 0:
        insert_stmt = sql.SQL(
            "INSERT INTO {tgt} SELECT * FROM {src}"
        ).format(
            tgt=sql.Identifier(target_table),
            src=sql.Identifier(source_table),
        )
        logger.debug("SQL: %s", insert_stmt.as_string(cursor))
        cursor.execute(insert_stmt)


# ──────────────────────────────────────────────────────────
# Overall status helpers
# ──────────────────────────────────────────────────────────

def _compute_table_status(action_results: list[dict]) -> str:
    """Derive table-level status from action results."""
    if not action_results:
        return "success"

    statuses = {r["status"] for r in action_results}
    if "failed" not in statuses:
        return "success"
    if all(r["status"] == "failed" for r in action_results):
        return "failed"
    return "partial"


def _compute_overall_status(table_results: list[dict]) -> str:
    """Derive top-level report status from table results."""
    if not table_results:
        return "success"

    statuses = {t["status"] for t in table_results}
    if "failed" not in statuses:
        return "success"
    if all(t["status"] == "failed" for t in table_results):
        return "failed"
    return "partial"


# ──────────────────────────────────────────────────────────
# Public entry point
# ──────────────────────────────────────────────────────────

def run_cleaning_plan(connection_string: str, plan: dict) -> dict:
    """
    Execute the AI-generated cleaning plan against the user's PostgreSQL database.

    Args:
        connection_string: The user's PostgreSQL DSN (e.g. postgresql://…).
                           This is the connection stored in the MongoDB job document,
                           NOT Django's own database.
        plan: The cleaning_plan dict fetched from the MongoDB analysis result.
              Expected shape: { "tables": [ { "table", "target_table", "actions" } ] }

    Returns:
        A structured execution report::

            {
                "status": "success" | "partial" | "failed",
                "tables": [
                    {
                        "table": "…",
                        "target_table": "…",
                        "status": "success" | "partial" | "failed",
                        "actions": [
                            {
                                "type": "…",
                                "column": "…",
                                "status": "success" | "skipped_needs_input" | "failed",
                                "rows_affected": 0,
                                "error": null
                            }
                        ]
                    }
                ]
            }
    """
    tables = plan.get("tables", [])
    table_results: list[dict] = []

    for table_spec in tables:
        source_table = table_spec.get("table", "")
        target_table = table_spec.get("target_table", "")
        actions = table_spec.get("actions", [])

        action_results: list[dict] = []

        # Each table gets its own connection + transaction so a failure in one
        # table never blocks the others.
        try:
            conn = psycopg2.connect(connection_string)
            conn.autocommit = False  # explicit transaction control
        except Exception as conn_err:
            logger.error(
                "Could not connect to user database for table %r: %s",
                source_table,
                conn_err,
            )
            # Mark every action as failed
            for action in actions:
                action_results.append({
                    "type": action.get("type"),
                    "column": action.get("column"),
                    "status": "failed",
                    "rows_affected": 0,
                    "error": f"Connection error: {conn_err}",
                })
            table_results.append({
                "table": source_table,
                "target_table": target_table,
                "status": "failed",
                "actions": action_results,
            })
            continue

        try:
            with conn:
                with conn.cursor() as cur:
                    # Step 1: materialise the target table from the source
                    try:
                        _create_target_table(cur, source_table, target_table)
                    except Exception as create_err:
                        logger.error(
                            "Failed to create target table %r from %r: %s",
                            target_table,
                            source_table,
                            create_err,
                        )
                        conn.rollback()
                        for action in actions:
                            action_results.append({
                                "type": action.get("type"),
                                "column": action.get("column"),
                                "status": "failed",
                                "rows_affected": 0,
                                "error": f"Table creation error: {create_err}",
                            })
                        table_results.append({
                            "table": source_table,
                            "target_table": target_table,
                            "status": "failed",
                            "actions": action_results,
                        })
                        continue

                    # Step 2: execute each action
                    for action in actions:
                        action_type = action.get("type")
                        column = action.get("column")
                        params = action.get("params") or {}

                        compiled = _compile_action(action, target_table)

                        if compiled is None:
                            # requires_user_input or unknown type
                            logger.info(
                                "Skipping action %r on column %r (table %r): needs user input or unsupported",
                                action_type,
                                column,
                                target_table,
                            )
                            action_results.append({
                                "type": action_type,
                                "column": column,
                                "status": "skipped_needs_input",
                                "rows_affected": 0,
                                "error": None,
                            })
                            continue

                        # Execute each SQL statement in the compiled list
                        total_rows = 0
                        action_failed = False
                        action_error = None

                        for stmt in compiled:
                            try:
                                logger.debug(
                                    "SQL [%s.%s]: %s",
                                    target_table,
                                    action_type,
                                    stmt.as_string(cur),
                                )
                                cur.execute(stmt)
                                rows = cur.rowcount if cur.rowcount >= 0 else 0
                                total_rows += rows
                            except Exception as exec_err:
                                logger.error(
                                    "Action %r on column %r (table %r) failed: %s",
                                    action_type,
                                    column,
                                    target_table,
                                    exec_err,
                                )
                                action_failed = True
                                action_error = str(exec_err)
                                # Roll back just this table's transaction and stop
                                conn.rollback()
                                break

                        if action_failed:
                            action_results.append({
                                "type": action_type,
                                "column": column,
                                "status": "failed",
                                "rows_affected": 0,
                                "error": action_error,
                            })
                        else:
                            action_results.append({
                                "type": action_type,
                                "column": column,
                                "status": "success",
                                "rows_affected": total_rows,
                                "error": None,
                            })

                    # Commit the whole table's worth of actions
                    conn.commit()

        except Exception as table_err:
            logger.error(
                "Unexpected error processing table %r: %s",
                source_table,
                table_err,
            )
            # Append a catch-all failure if we somehow exit unexpectedly
            if not action_results:
                for action in actions:
                    action_results.append({
                        "type": action.get("type"),
                        "column": action.get("column"),
                        "status": "failed",
                        "rows_affected": 0,
                        "error": str(table_err),
                    })
        finally:
            try:
                conn.close()
            except Exception:
                pass

        table_results.append({
            "table": source_table,
            "target_table": target_table,
            "status": _compute_table_status(action_results),
            "actions": action_results,
        })

    overall_status = _compute_overall_status(table_results)
    logger.info(
        "Cleaning plan execution finished — overall status: %s (%d tables)",
        overall_status,
        len(table_results),
    )
    return {
        "status": overall_status,
        "tables": table_results,
    }
