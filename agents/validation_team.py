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
    # AGENT 4 — Data Cleaning Planner
    # ------------------------------------------------------------------
    logger.info("Starting Data Cleaning Planner (Agno / %s)...", _GEMINI_MODEL_ID)
    _emit('running_cleaning_plan')

    cleaning_agent = Agent(
        name="Data Cleaning Planner",
        model=_make_model(),
        description="Generates a deterministic JSON cleaning plan based on validation reports.",
        instructions=[
            "You are a data cleaning planner.",
            "Your job is to convert validation findings into a structured cleaning plan.",
            "STRICT RULES:",
            "- Output ONLY valid JSON",
            "- No markdown, no explanations, no extra text",
            "- Do NOT execute SQL",
            "- Do NOT suggest — decide deterministically",
            "FORMAT:",
            """
            {
              \"tables\": [
                {
                  \"table\": \"string\",
                  \"target_table\": \"string\",
                  \"actions\": [
                    {
                      \"type\": \"cast | fill_null | drop_duplicates | drop_column\",
                      \"column\": \"string | null\",
                      \"params\": {}
                    }
                  ]
                }
              ]
            }
            """,
            "CRITICAL RULES:",
            "- ALWAYS use shadow tables: target_table = table + '_clean'",
            "- Generate plan for ALL tables found in schema",
            "- If decision is ambiguous → add: \"requires_user_input\": true inside params",
            "CLEANING RULES:",
            "- Mixed types → cast, invalid → NULL",
            "- Numeric nulls → median",
            "- Categorical nulls → mode",
            "- Duplicate rows → remove using best candidate columns",
            "- If null ratio > 0.6 → drop_column",
            "ORDER:",
            "1. cast",
            "2. fill_null",
            "3. drop_duplicates",
            "4. drop_column",
        ],
        tools=[],  # No SQL access — planner only
        markdown=False,
    )

    cleaning_prompt = (
        "Generate a full cleaning plan.\n\n"
        "--- Schema Findings ---\n"
        + schema_report
        + "\n\n--- Cross-Row Findings ---\n"
        + cross_report
        + "\n\n--- Row-Level Findings ---\n"
        + row_report
    )

    cleaning_plan_raw = _run_agent(cleaning_agent, cleaning_prompt)
    logger.info("Data Cleaning Planner finished.")

    try:
        cleaning_plan = json.loads(cleaning_plan_raw)
    except Exception:
        logger.error("Invalid JSON from cleaning agent:\n%s", cleaning_plan_raw)
        # Wrap raw text so the pipeline never crashes
        cleaning_plan = {"error": "Cleaning plan could not be parsed", "raw": cleaning_plan_raw}

    return {
        "schema_issues": schema_report,
        "cross_row_issues": cross_report,
        "row_issues": row_report,
        "cleaning_plan": cleaning_plan,
    }
