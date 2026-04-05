"""
PostgreSQL Validation Multi-Agent Team — powered by Agno + Gemini Flash.

Three specialised agents run sequentially:
  1. Schema Integrity Validator  – explores DB structure.
  2. Cross-Row Validator         – detects aggregate / referential violations.
  3. Row-Level Logic Validator   – hunts individual row failures.

The `orchestrator_workflow()` function is the public entry point consumed by
`services/postgres_service.py` (and therefore the Celery task).
Its signature is unchanged from the previous implementation.
"""

import json
import logging
import os

from agno.agent import Agent
from agno.models.google import Gemini

from .tools import execute_pg_query, postgres_tool
from .agent_config import (
    SCHEMA_DESCRIPTION, SCHEMA_INSTRUCTIONS,
    CROSS_ROW_DESCRIPTION, CROSS_ROW_INSTRUCTIONS,
    ROW_LEVEL_DESCRIPTION, ROW_LEVEL_INSTRUCTIONS,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Model — gemini-2.0-flash is the stable GA release; change via env if needed.
# ---------------------------------------------------------------------------
_GEMINI_MODEL_ID = os.environ.get("GEMINI_MODEL", "gemini-2.0-flash")
_GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")


def _make_model() -> Gemini:
    """Return a fresh Gemini model instance (avoids shared state between runs)."""
    return Gemini(id=_GEMINI_MODEL_ID, api_key=_GEMINI_API_KEY or None)


# ---------------------------------------------------------------------------
# Helper: run one agent synchronously and return its text output
# ---------------------------------------------------------------------------
def _run_agent(agent: Agent, prompt: str) -> str:
    """Run an Agno agent and extract its text response."""
    try:
        response = agent.run(prompt)
        # Agno returns a RunResponse; .content holds the final text
        if hasattr(response, "content") and response.content:
            return response.content
        # Fallback: stringify the whole object
        return str(response)
    except Exception as exc:
        logger.error("Agent '%s' failed: %s", agent.name, exc)
        return f"Agent failed with error: {exc}"


# ---------------------------------------------------------------------------
# Public entry point — signature unchanged from the old implementation
# ---------------------------------------------------------------------------
def orchestrator_workflow(connection_string: str, domain_rules: str, job_id: str | None = None) -> dict:
    """
    Main entry point for Celery tasks. Runs the three-agent validation pipeline
    sequentially and returns a dict with markdown reports for each phase.

    Args:
        connection_string: PostgreSQL DSN, e.g. 'postgresql://user:pass@host/db'.
        domain_rules: Free-text business rules formatted by `postgres_service.py`.
        job_id: Optional MongoDB job ID. When provided, intermediate statuses are
                written to the DB after each agent so the frontend can show
                realistic per-agent progress.

    Returns:
        {
            "schema_issues":    <markdown str>,
            "cross_row_issues": <markdown str>,
            "row_issues":       <markdown str>,
        }
    """
    from api.mongo import update_job_status   # avoid circular import at module level

    def _emit(status: str):
        if job_id:
            try:
                update_job_status(job_id, status)
            except Exception:
                pass  # never let status updates crash the pipeline

    # 1. Wire up the DB tool for this run
    postgres_tool.connect(connection_string)

    # 2. Build a common context preamble that every agent receives
    rules_section = (
        f"\n\n---\n**Domain/Business Rules to Enforce:**\n{domain_rules}"
        if domain_rules
        else ""
    )

    # ------------------------------------------------------------------
    # AGENT 1 — Schema Integrity Validator
    # ------------------------------------------------------------------
    logger.info("Starting Schema Integrity Validator (Agno / %s)...", _GEMINI_MODEL_ID)
    _emit('running_schema')
    schema_agent = Agent(
        name="Schema Integrity Validator",
        model=_make_model(),
        description=SCHEMA_DESCRIPTION,
        instructions=SCHEMA_INSTRUCTIONS,
        tools=[execute_pg_query],
        markdown=True,
    )
    schema_prompt = (
        "Please execute your role. Explore the database schema and produce a full "
        "markdown report covering: table list, column types, primary/foreign keys, "
        "and any structural issues you detect."
        + rules_section
    )
    schema_report = _run_agent(schema_agent, schema_prompt)
    logger.info("Schema Validator finished.")

    # ------------------------------------------------------------------
    # AGENT 2 — Cross-Row Validator
    # ------------------------------------------------------------------
    logger.info("Starting Cross-Row Validator (Agno / %s)...", _GEMINI_MODEL_ID)
    _emit('running_cross_row')
    cross_agent = Agent(
        name="Cross-Row Validator",
        model=_make_model(),
        description=CROSS_ROW_DESCRIPTION,
        instructions=CROSS_ROW_INSTRUCTIONS,
        tools=[execute_pg_query],
        markdown=True,
    )
    cross_prompt = (
        "Please execute your role. Use the schema findings below as your reference "
        "for real table/column names and produce a markdown cross-row report.\n\n"
        "--- Schema Findings ---\n"
        + schema_report
        + rules_section
    )
    cross_report = _run_agent(cross_agent, cross_prompt)
    logger.info("Cross-Row Validator finished.")

    # ------------------------------------------------------------------
    # AGENT 3 — Row-Level Logic Validator
    # ------------------------------------------------------------------
    logger.info("Starting Row-Level Logic Validator (Agno / %s)...", _GEMINI_MODEL_ID)
    _emit('running_row_level')
    row_agent = Agent(
        name="Row Logic Validator",
        model=_make_model(),
        description=ROW_LEVEL_DESCRIPTION,
        instructions=ROW_LEVEL_INSTRUCTIONS,
        tools=[execute_pg_query],
        markdown=True,
    )
    row_prompt = (
        "Please execute your role. Use the context below and produce a markdown "
        "row-level validation report.\n\n"
        "--- Schema Findings ---\n"
        + schema_report
        + "\n\n--- Cross-Row Findings ---\n"
        + cross_report
        + rules_section
    )
    row_report = _run_agent(row_agent, row_prompt)
    logger.info("Row Logic Validator finished.")

    # ------------------------------------------------------------------
    # AGENT 4 — Fix Planner (human-in-the-loop)
    # ------------------------------------------------------------------
    logger.info("Starting Fix Planner (Agno / %s)...", _GEMINI_MODEL_ID)
    _emit('running_fix_planner')

    fix_planner = Agent(
        name="Fix Planner",
        model=_make_model(),
        description=(
            "You are a PostgreSQL data quality fix planner. "
            "You read validation reports and produce a structured list of "
            "human-reviewable fix cards that a data engineer can Accept or Reject one by one."
        ),
        instructions=[
            # ── Output contract ────────────────────────────────────────
            "Output ONLY valid JSON — no markdown fences, no prose, no comments.",
            "The JSON must match this schema exactly:",
            """
{
  "fixes": [
    {
      "id": "fix_001",
      "table": "<source table name>",
      "target_table": "<source table name>_clean",
      "problem": "<concise plain-English description of the data quality issue>",
      "fix": "<one-line title of the proposed fix>",
      "queries": [
        "<SQL statement 1>",
        "<SQL statement 2>"
      ],
      "explanation": "<plain-English explanation of why these queries fix the problem>"
    }
  ]
}
""",
            # ── Safety rules (CRITICAL) ────────────────────────────────
            "SAFETY — NEVER modify source tables. Every DML query (UPDATE, DELETE, INSERT) "
            "MUST target only <table>_clean tables. Violating this is a critical error.",
            # ── Setup pattern ─────────────────────────────────────────
            "The first two queries of EVERY fix card must always be:",
            "  1. CREATE TABLE IF NOT EXISTS <table>_clean (LIKE <table> INCLUDING ALL);",
            "  2. INSERT INTO <table>_clean SELECT * FROM <table> ON CONFLICT DO NOTHING;",
            "These ensure the clean shadow table exists and is populated before the fix is applied.",
            # ── Fix generation rules ───────────────────────────────────
            "Only generate fix cards for REAL issues found in the validation reports.",
            "Do NOT generate fix cards for tables that have no reported problems.",
            "One fix card = one distinct, specific problem.",
            "If fixing a problem requires multiple SQL statements, include all of them in 'queries'.",
            "Use concrete column names and table names from the reports — never placeholders.",
            # ── SQL rules ─────────────────────────────────────────────
            "NULL filling — categorical columns: use mode (most frequent non-null value).",
            "NULL filling — numeric columns: use median (percentile_cont(0.5)).",
            "Duplicate removal: use DELETE … WHERE ctid NOT IN (SELECT MIN(ctid) …).",
            "Type cast: use ALTER TABLE <table>_clean ALTER COLUMN … TYPE … USING …::type.",
            "Invalid value correction: use UPDATE <table>_clean SET … WHERE <condition>.",
            # ── Content rules ──────────────────────────────────────────
            "The 'problem' field must be plain English — no SQL, no jargon.",
            "The 'fix' field is a short action title, e.g. 'Fill NULL emails with mode'.",
            "The 'explanation' field must explain WHY this fix is appropriate for non-technical readers.",
            # ── ID format ─────────────────────────────────────────────
            "Assign sequential IDs: fix_001, fix_002, fix_003 …",
        ],
        tools=[],   # planner only — no DB access
        markdown=False,
    )

    fix_prompt = (
        "Generate a list of human-reviewable fix cards for all data quality issues found below.\n\n"
        "--- Schema Findings ---\n"
        + schema_report
        + "\n\n--- Cross-Row Findings ---\n"
        + cross_report
        + "\n\n--- Row-Level Findings ---\n"
        + row_report
    )

    fixes_raw = _run_agent(fix_planner, fix_prompt)
    logger.info("Fix Planner finished.")

    # Strip markdown code fences if the model added them despite instructions
    fixes_raw_stripped = fixes_raw.strip()
    if fixes_raw_stripped.startswith("```"):
        fixes_raw_stripped = fixes_raw_stripped.split("\n", 1)[-1]
        fixes_raw_stripped = fixes_raw_stripped.rsplit("```", 1)[0]

    try:
        fixes_parsed = json.loads(fixes_raw_stripped)
    except Exception:
        logger.error("Invalid JSON from Fix Planner:\n%s", fixes_raw)
        fixes_parsed = {
            "error": "Fix plan could not be parsed — JSON was invalid",
            "raw": fixes_raw,
        }

    return {
        "schema_analysis":   {"report": schema_report},
        "cross_row_analysis": {"report": cross_report},
        "row_level_analysis": {"report": row_report},
        "fixes": fixes_parsed,
    }
