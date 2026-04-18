"""
AgentOS entry-point — exposes a single QualityLab Validation Team in the
browser playground via Agno's AgentOS / FastAPI layer.

Usage:
  cd backend-django/agents
  python agent_os.py

Then open https://os.agno.com and connect to http://localhost:7777,
OR browse the auto-generated REST API at http://localhost:7777/docs.

Talk to the team master directly:
  "Connect to postgresql://user:pass@localhost:5432/mydb and validate everything."

The master agent will coordinate four specialist sub-agents:
  1. Schema Integrity Validator  — structural issues
    2. Column Validator            — single-column quality/distribution issues
    3. Row-Level Logic Validator   — per-row business rule failures
    4. Cross-Row Validator         — multi-row / relational anomalies

All agents share the same SqliteDb session so they have full conversation memory.

NOTE: The AgentOS playground uses the Team coordinate mode where the master
delegates autonomously. The GuidelineGeneratorAgent is integrated into the
Celery pipeline only (validation_team.py). In the playground, agents use the
cleaned-up, database-agnostic base instructions and discover the schema
organically through their own tool calls.
"""

import os
import sys
from typing import Any

# Allow running this file directly from the agents/ folder
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

try:
    from agents.tools import connect_to_database, execute_pg_query  # as module
    from agents.agent_config import (
        SCHEMA_DESCRIPTION, SCHEMA_INSTRUCTIONS,
        COLUMN_VALIDATOR_DESCRIPTION, COLUMN_VALIDATOR_INSTRUCTIONS,
        CROSS_ROW_DESCRIPTION, CROSS_ROW_INSTRUCTIONS,
        ROW_LEVEL_DESCRIPTION, ROW_LEVEL_INSTRUCTIONS,
    )
except ImportError:
    from tools import connect_to_database, execute_pg_query           # as script
    from agent_config import (
        SCHEMA_DESCRIPTION, SCHEMA_INSTRUCTIONS,
        COLUMN_VALIDATOR_DESCRIPTION, COLUMN_VALIDATOR_INSTRUCTIONS,
        CROSS_ROW_DESCRIPTION, CROSS_ROW_INSTRUCTIONS,
        ROW_LEVEL_DESCRIPTION, ROW_LEVEL_INSTRUCTIONS,
    )

from agno.agent import Agent
from agno.db.sqlite import SqliteDb
from agno.models.google import Gemini
from agno.os import AgentOS
from agno.team import Team
from agno.team.mode import TeamMode
from dotenv import load_dotenv

load_dotenv()

_DEFAULT_GEMINI_MODEL_ID = os.environ.get("GEMINI_MODEL", "gemini-2.0-flash")
_GEMINI_API_KEY  = os.environ.get("GEMINI_API_KEY", "")

# ---------------------------------------------------------------------------
# Shared session DB — passed to BOTH the Team and every member agent so they
# all share the same conversation history and memory store.
# ---------------------------------------------------------------------------
_SESSION_DB = SqliteDb(db_file="tmp/agentos_sessions.db")


def _resolve_model_id(requested_model: str | None = None) -> str:
    model_id = (requested_model or "").strip()
    return model_id or _DEFAULT_GEMINI_MODEL_ID


def _model(model_id: str | None = None) -> Gemini:
    return Gemini(
        id=_resolve_model_id(model_id),
        api_key=_GEMINI_API_KEY or None,
        temperature=0,
        top_p=1,
        seed=7,
    )


def _set_member_model(member: Agent | Team, model_id: str) -> None:
    member.model = _model(model_id)
    if isinstance(member, Team):
        for nested_member in member.members:
            _set_member_model(nested_member, model_id)


def _apply_requested_llm_model(
    team: Team,
    llm_model: str | None = None,
    model_id: str | None = None,
    run_context: Any | None = None,
    **_: Any,
) -> None:
    """Allow per-run Gemini model override coming from frontend chat calls."""
    selected_model_id = _resolve_model_id(llm_model or model_id)
    team.model = _model(selected_model_id)

    for member in team.members:
        _set_member_model(member, selected_model_id)

    if run_context is not None:
        metadata = dict(run_context.metadata or {})
        metadata["llm_model"] = selected_model_id
        run_context.metadata = metadata


# Prepended to playground agents so they call connect_to_database first
_CONNECT_RULE = (
    "When the user provides a connection string, ALWAYS call connect_to_database "
    "FIRST before running any SQL queries."
)

# Tools shared by all sub-agents
_TOOLS = [connect_to_database, execute_pg_query]

# ---------------------------------------------------------------------------
# Sub-agent 1 — Schema Integrity Validator
# ---------------------------------------------------------------------------
schema_agent = Agent(
    id="schema-validator",
    name="Schema Integrity Validator",
    role=(
        "Analyses the database structure: missing PKs, unlinked FKs, "
        "suspicious nullability, naming issues. Works on any PostgreSQL database."
    ),
    model=_model(),
    description=SCHEMA_DESCRIPTION,
    instructions=[_CONNECT_RULE] + SCHEMA_INSTRUCTIONS,
    tools=_TOOLS,
    db=_SESSION_DB,
    add_history_to_context=True,
    num_history_runs=3,
    add_datetime_to_context=True,
    markdown=True,
)

# ---------------------------------------------------------------------------
# Sub-agent 2 — Column Validator
#
# Ownership boundary:
# - OWNS: single-column distribution/semantic anomalies.
# - DEFERS: multi-row, temporal, and sequential anomalies to Cross-Row.
# ---------------------------------------------------------------------------
column_agent = Agent(
    id="column-validator",
    name="Column Validator",
    role=(
        "Detects column-level anomalies: null/cardinality drift, near-constant behavior, "
        "type-distribution mismatches, and semantic contradictions from column names. "
        "Works on any PostgreSQL database."
    ),
    model=_model(),
    description=COLUMN_VALIDATOR_DESCRIPTION,
    instructions=[_CONNECT_RULE] + COLUMN_VALIDATOR_INSTRUCTIONS,
    tools=_TOOLS,
    db=_SESSION_DB,
    add_history_to_context=True,
    num_history_runs=3,
    add_datetime_to_context=True,
    markdown=True,
)

# ---------------------------------------------------------------------------
# Sub-agent 3 — Row-Level Logic Validator
# ---------------------------------------------------------------------------
row_agent = Agent(
    id="row-level-validator",
    name="Row-Level Logic Validator",
    role=(
        "Validates individual rows for impossible dates, blank required fields, "
        "bad formats, and domain rule violations. Works on any PostgreSQL database."
    ),
    model=_model(),
    description=ROW_LEVEL_DESCRIPTION,
    instructions=[_CONNECT_RULE] + ROW_LEVEL_INSTRUCTIONS,
    tools=_TOOLS,
    db=_SESSION_DB,
    add_history_to_context=True,
    num_history_runs=3,
    add_datetime_to_context=True,
    markdown=True,
)

# ---------------------------------------------------------------------------
# Sub-agent 4 — Cross-Row Validator
#
# Ownership boundary:
# - OWNS: multi-row, temporal/sequential, and relational anomalies.
# - DEFERS: single-column distribution anomalies to Column Validator.
# ---------------------------------------------------------------------------
cross_row_agent = Agent(
    id="cross-row-validator",
    name="Cross-Row Validator",
    role=(
        "Detects aggregate violations: orphaned FKs, duplicate unique values, "
        "cross-table consistency problems. Works on any PostgreSQL database."
    ),
    model=_model(),
    description=CROSS_ROW_DESCRIPTION,
    instructions=[_CONNECT_RULE] + CROSS_ROW_INSTRUCTIONS,
    tools=_TOOLS,
    db=_SESSION_DB,
    add_history_to_context=True,
    num_history_runs=3,
    add_datetime_to_context=True,
    markdown=True,
)

# ---------------------------------------------------------------------------
# Master Team — the single entry point for the user
#
# mode=TeamMode.coordinate  → the master decides which sub-agents to call
#                             and synthesises their outputs into one report.
# add_team_history_to_members=True  → sub-agents see the full team history.
# show_members_responses=True       → intermediate sub-agent reports are visible.
# ---------------------------------------------------------------------------
validation_team = Team(
    id="qualitylab-validation-team",
    name="QualityLab Validation Team",
    mode=TeamMode.coordinate,
    model=_model(),
    members=[schema_agent, column_agent, row_agent, cross_row_agent],
    description=(
        "An AI-powered data-quality team for any PostgreSQL database. "
        "Give me a connection string and I will coordinate four specialist agents "
        "to validate schema structure, column-level quality, row-level logic, "
        "and cross-row integrity, then deliver a unified report."
    ),
    instructions=[
        "When the user provides a PostgreSQL connection string, call connect_to_database first.",
        "Then delegate tasks in this order:",
        "  1. Ask the Schema Integrity Validator to map and check the database structure.",
        "  2. Pass schema findings to the Column Validator for column-level quality checks.",
        "  3. Pass schema+column findings to the Row-Level Logic Validator for per-row rules.",
        "  4. Pass schema+column+row findings to the Cross-Row Validator for multi-row checks.",
        "Finally, synthesise all four reports into a single, well-structured markdown document "
        "with sections: ## Schema Issues, ## Column Issues, ## Row-Level Issues, ## Cross-Row Issues.",
        "Every final answer must be valid GitHub-flavored Markdown.",
        "Keep tone professional, friendly, and solution-oriented.",
        "When discussing problems, always include concise sections for Problem, Impact, and Recommendation.",
        "Close each response with a short Next Steps section.",
        "Do not return raw JSON unless the user explicitly asks for JSON output.",
        "If the user asks about only one aspect (e.g. 'check only the schema'), "
        "delegate only the relevant agent.",
        "You are database-agnostic — discover the schema first before running any checks.",
    ],
    pre_hooks=[_apply_requested_llm_model],
    tools=_TOOLS,              # team leader also has direct tool access if needed
    db=_SESSION_DB,            # shared memory with sub-agents
    add_team_history_to_members=True,
    show_members_responses=False,
    add_datetime_to_context=True,
    markdown=True,
)

# ---------------------------------------------------------------------------
# AgentOS — exposes the team (not individual agents) in the playground
# ---------------------------------------------------------------------------
agent_os = AgentOS(
    name="QualityLab Validation Agents",
    description="AI-powered PostgreSQL data-quality validation suite — works with any PostgreSQL database.",
    teams=[validation_team],
)

app = agent_os.get_app()

if __name__ == "__main__":
    os.makedirs("tmp", exist_ok=True)
    agent_os.serve(app="agent_os:app", reload=True, port=7777)
