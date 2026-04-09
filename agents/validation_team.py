"""
PostgreSQL Validation Multi-Agent Team — powered by Agno + Gemini Flash.

Pipeline (database-agnostic, dynamic guidelines):

  GuidelineGenerator → Schema Integrity Validator
        ↓ (schema findings passed back)
  GuidelineGenerator → Cross-Row Validator
        ↓ (accumulated findings passed back)
  GuidelineGenerator → Row-Level Logic Validator
        ↓ (accumulated findings passed back)
  GuidelineGenerator → Data Fix Planner
        ↓
  Persist new guidelines version to guidelines_store/

Before each downstream agent, the GuidelineGeneratorAgent introspects the live
database and produces tailored, database-specific guidelines that replace the
previous hardcoded EasyBulk-specific instructions.

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
from .guideline_generator import DatabaseGuidelineGeneratorAgent
from .agent_config import (
    SCHEMA_DESCRIPTION, SCHEMA_INSTRUCTIONS,
    CROSS_ROW_DESCRIPTION, CROSS_ROW_INSTRUCTIONS,
    ROW_LEVEL_DESCRIPTION, ROW_LEVEL_INSTRUCTIONS,
    FIX_PLANNER_DESCRIPTION, FIX_PLANNER_INSTRUCTIONS,
    GUIDELINES_INJECTION_TEMPLATE,
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


def _inject_guidelines(guidelines: str) -> str:
    """Format guidelines using the shared injection template."""
    return GUIDELINES_INJECTION_TEMPLATE.format(guidelines=guidelines)


# ---------------------------------------------------------------------------
# Score helpers
# ---------------------------------------------------------------------------

# Keywords that signal the agent found a real issue
_ISSUE_KEYWORDS = [
    "error", "invalid", "missing", "null", "duplicate", "mismatch",
    "violation", "constraint", "inconsistent", "orphan", "incorrect",
    "warning", "fail", "anomaly", "problem", "issue",
]


def _compute_scores(
    schema_report: str,
    cross_report: str,
    row_report: str,
    fixes_parsed: object,
) -> tuple[int, int]:
    """
    Derive two 0-100 integer scores from the agent outputs.

    **Confidence Score** — how thoroughly the agents explored the database.
    It is based on the combined length of the three markdown reports.
    Longer, more detailed reports mean higher coverage confidence.

    **Data Quality Score** — how healthy the data looks.
    Starts at 100 and is penalised for:
      - issue-signalling keywords found in the reports
      - the number of fix cards produced by the Fix Planner

    Both scores are clamped to [0, 100].
    """
    combined = f"{schema_report}\n{cross_report}\n{row_report}"
    word_count = len(combined.split())

    # ── Confidence: coverage proxy ──────────────────────────────────────
    # 2 000 words → ~80 pts; scales linearly, capped at 95
    raw_conf = min(word_count / 2_500, 1.0) * 95
    confidence_score = max(30, int(round(raw_conf)))  # floor at 30 (agents ran)

    # ── Data Quality: issue-density penalty ──────────────────────────────
    combined_lower = combined.lower()
    issue_hits = sum(combined_lower.count(kw) for kw in _ISSUE_KEYWORDS)

    # Fix-card count penalty: each fix card costs 3 pts
    fix_count = 0
    if isinstance(fixes_parsed, list):
        fix_count = len(fixes_parsed)
    elif isinstance(fixes_parsed, dict) and isinstance(fixes_parsed.get("fixes"), list):
        fix_count = len(fixes_parsed["fixes"])

    # Normalise: 50 issue hits → ~20 pt penalty; 1 000 hits → ~40 pt penalty
    keyword_penalty = min(int(issue_hits / 1.3), 40)
    fix_penalty     = min(fix_count * 3, 30)

    data_quality_score = max(0, min(100, 100 - keyword_penalty - fix_penalty))

    return confidence_score, data_quality_score


# ---------------------------------------------------------------------------
# Public entry point — signature unchanged from the old implementation
# ---------------------------------------------------------------------------
def orchestrator_workflow(
    connection_string: str, domain_rules: str, job_id: str | None = None
) -> dict:
    """
    Main entry point for Celery tasks. Runs the four-agent validation pipeline
    sequentially and returns a dict with reports and fix cards.

    The pipeline is fully database-agnostic: a GuidelineGeneratorAgent runs before
    each of the four downstream agents, introspects the live database, and produces
    tailored guidelines. No hardcoded assumptions about schema, tables, or columns.

    Args:
        connection_string: PostgreSQL DSN, e.g. 'postgresql://user:pass@host/db'.
        domain_rules: Free-text business rules formatted by `postgres_service.py`.
        job_id: Optional MongoDB job ID. When provided, intermediate statuses are
                written to the DB after each agent so the frontend can show
                realistic per-agent progress.

    Returns:
        {
            "schema_analysis":    <markdown str>,
            "cross_row_analysis": <markdown str>,
            "row_level_analysis": <markdown str>,
            "fixes":              <parsed dict | error dict>,
        }
    """
    from api.db import update_job_status, save_job_guideline  # avoid circular import

    def _emit(status: str):
        if job_id:
            try:
                update_job_status(job_id, status)
            except Exception:
                pass  # never let status updates crash the pipeline

    def _save_guideline(agent_key: str, text: str):
        """Persist a generated guideline to Mongo so the frontend can view it."""
        if job_id:
            try:
                save_job_guideline(job_id, agent_key, text)
            except Exception:
                pass  # never let persistence crash the pipeline

    # ------------------------------------------------------------------
    # 0. Wire up the DB tool and instantiate the GuidelineGeneratorAgent
    # ------------------------------------------------------------------
    postgres_tool.connect(connection_string)

    generator = DatabaseGuidelineGeneratorAgent(
        connection_string=connection_string,
        model_id=_GEMINI_MODEL_ID,
        api_key=_GEMINI_API_KEY,
    )

    # Optional domain rules section appended to every agent prompt
    rules_section = (
        f"\n\n---\n**Domain/Business Rules to Enforce:**\n{domain_rules}"
        if domain_rules
        else ""
    )

    # Accumulated findings — grows after each agent and is passed back to the
    # GuidelineGenerator so each new set of guidelines can build on prior work.
    prior_findings = ""

    # ------------------------------------------------------------------
    # AGENT 1 — Schema Integrity Validator
    # ------------------------------------------------------------------
    logger.info(
        "GuidelineGenerator: generating guidelines for schema_integrity_validator..."
    )
    _emit("running_guideline_gen_schema")
    schema_guidelines = generator.generate(
        target_agent="schema_integrity_validator",
        prior_findings=prior_findings,
    )
    _save_guideline("schema_integrity_validator", schema_guidelines)

    logger.info("Starting Schema Integrity Validator (Agno / %s)...", _GEMINI_MODEL_ID)
    _emit("running_schema")
    schema_agent = Agent(
        name="Schema Integrity Validator",
        model=_make_model(),
        description=SCHEMA_DESCRIPTION,
        instructions=SCHEMA_INSTRUCTIONS,
        tools=[execute_pg_query],
        tool_call_limit=20,  # AFC cap — override default of 10
        markdown=True,
    )
    schema_prompt = (
        _inject_guidelines(schema_guidelines)
        + "Please execute your role. Explore the database schema and produce a full "
        "markdown report covering: table list, column types, primary/foreign keys, "
        "and any structural issues you detect."
        + rules_section
    )
    schema_report = _run_agent(schema_agent, schema_prompt)
    logger.info("Schema Validator finished.")

    # Accumulate findings for next generator call
    prior_findings = f"## Schema Integrity Findings\n\n{schema_report}"

    # ------------------------------------------------------------------
    # AGENT 2 — Cross-Row Validator
    # ------------------------------------------------------------------
    logger.info(
        "GuidelineGenerator: generating guidelines for cross_row_validator..."
    )
    _emit("running_guideline_gen_cross_row")
    cross_guidelines = generator.generate(
        target_agent="cross_row_validator",
        prior_findings=prior_findings,
    )
    _save_guideline("cross_row_validator", cross_guidelines)

    logger.info("Starting Cross-Row Validator (Agno / %s)...", _GEMINI_MODEL_ID)
    _emit("running_cross_row")
    cross_agent = Agent(
        name="Cross-Row Validator",
        model=_make_model(),
        description=CROSS_ROW_DESCRIPTION,
        instructions=CROSS_ROW_INSTRUCTIONS,
        tools=[execute_pg_query],
        tool_call_limit=20,  # AFC cap — override default of 10
        markdown=True,
    )
    cross_prompt = (
        _inject_guidelines(cross_guidelines)
        + "Please execute your role. Use the schema findings below as your reference "
        "for real table/column names and produce a markdown cross-row report.\n\n"
        "--- Schema Findings ---\n"
        + schema_report
        + rules_section
    )
    cross_report = _run_agent(cross_agent, cross_prompt)
    logger.info("Cross-Row Validator finished.")

    # Accumulate findings
    prior_findings += f"\n\n## Cross-Row Findings\n\n{cross_report}"

    # ------------------------------------------------------------------
    # AGENT 3 — Row-Level Logic Validator
    # ------------------------------------------------------------------
    logger.info(
        "GuidelineGenerator: generating guidelines for row_level_validator..."
    )
    _emit("running_guideline_gen_row_level")
    row_guidelines = generator.generate(
        target_agent="row_level_validator",
        prior_findings=prior_findings,
    )
    _save_guideline("row_level_validator", row_guidelines)

    logger.info("Starting Row-Level Logic Validator (Agno / %s)...", _GEMINI_MODEL_ID)
    _emit("running_row_level")
    row_agent = Agent(
        name="Row Logic Validator",
        model=_make_model(),
        description=ROW_LEVEL_DESCRIPTION,
        instructions=ROW_LEVEL_INSTRUCTIONS,
        tools=[execute_pg_query],
        tool_call_limit=20,  # AFC cap — override default of 10
        markdown=True,
    )
    row_prompt = (
        _inject_guidelines(row_guidelines)
        + "Please execute your role. Use the context below and produce a markdown "
        "row-level validation report.\n\n"
        "--- Schema Findings ---\n"
        + schema_report
        + "\n\n--- Cross-Row Findings ---\n"
        + cross_report
        + rules_section
    )
    row_report = _run_agent(row_agent, row_prompt)
    logger.info("Row Logic Validator finished.")

    # Accumulate findings
    prior_findings += f"\n\n## Row-Level Findings\n\n{row_report}"

    # ------------------------------------------------------------------
    # AGENT 4 — Fix Planner (human-in-the-loop)
    # ------------------------------------------------------------------
    logger.info(
        "GuidelineGenerator: generating guidelines for data_fix_planner..."
    )
    _emit("running_guideline_gen_fix_planner")
    fix_guidelines = generator.generate(
        target_agent="data_fix_planner",
        prior_findings=prior_findings,
    )
    _save_guideline("data_fix_planner", fix_guidelines)
    logger.info("Starting Fix Planner (Agno / %s)...", _GEMINI_MODEL_ID)
    _emit("running_fix_planner")
    fix_planner = Agent(
        name="Fix Planner",
        model=_make_model(),
        description=FIX_PLANNER_DESCRIPTION,
        instructions=FIX_PLANNER_INSTRUCTIONS,
        tools=[],            # planner only — no direct DB access
        tool_call_limit=20,  # AFC cap — override default of 10
        markdown=False,
    )
    fix_prompt = (
        _inject_guidelines(fix_guidelines)
        + "Generate a list of human-reviewable fix cards for all data quality issues found below.\n\n"
        "--- Schema Findings ---\n"
        + schema_report
        + "\n\n--- Cross-Row Findings ---\n"
        + cross_report
        + "\n\n--- Row-Level Findings ---\n"
        + row_report
    )
    fixes_raw = _run_agent(fix_planner, fix_prompt)
    logger.info("Fix Planner finished.")

    # ------------------------------------------------------------------
    # 5. Persist guidelines for this run (new version appended, never overwrite)
    # ------------------------------------------------------------------
    logger.info("Persisting guidelines to guidelines_store/...")
    _emit("saving_guidelines")
    generator.save_guidelines()

    # ------------------------------------------------------------------
    # Parse Fix Planner output
    # ------------------------------------------------------------------
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

    # ------------------------------------------------------------------
    # 6. Compute Confidence Score & Data Quality Score
    # ------------------------------------------------------------------
    confidence_score, data_quality_score = _compute_scores(
        schema_report, cross_report, row_report, fixes_parsed
    )
    logger.info(
        "Scores — confidence: %d/100, data_quality: %d/100",
        confidence_score, data_quality_score,
    )

    return {
        "schema_analysis":    {"report": schema_report},
        "cross_row_analysis": {"report": cross_report},
        "row_level_analysis": {"report": row_report},
        "fixes": fixes_parsed,
        "confidence_score":   confidence_score,
        "data_quality_score": data_quality_score,
    }
