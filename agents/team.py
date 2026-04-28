"""
team.py - Shared QualityLab agent and team wiring.

This is the single source of truth for the specialist agents used by both the
deterministic workflow pipeline and the AgentOS playground.
"""

from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from agno.agent import Agent
from agno.db.sqlite import SqliteDb
from agno.guardrails.pii import PIIDetectionGuardrail
from agno.guardrails.prompt_injection import PromptInjectionGuardrail
from agno.models.google import Gemini
from agno.team import Team
from agno.team.mode import TeamMode

from .agent_config import (
    COLUMN_AGENT_CONFIG,
    CROSSROW_AGENT_CONFIG,
    FIX_PLANNER_AGENT_CONFIG,
    ROW_AGENT_CONFIG,
    SCHEMA_AGENT_CONFIG,
)
from .hooks import (
    ensure_schema_primed,
    inject_domain_rules,
    log_run_metrics,
    mark_run_start,
    prime_connection_from_prompt,
    qualitylab_tool_hook,
    validate_findings_entities,
    validate_fix_sql_safety,
)
from .rules import common_rules, domain_rules, policy_rules
from .tool_factories import column_tools, crossrow_tools, fixplanner_tools, row_tools, schema_tools

_BASE_DIR = Path(__file__).resolve().parent.parent
_AGENTOS_DB = SqliteDb(db_file=str(Path(__file__).resolve().parent / "tmp" / "agentos_sessions.db"))
_DEFAULT_MODEL_ID = os.environ.get("GEMINI_MODEL", "gemini-2.0-flash")
_GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
_MAX_OUTPUT_TOKENS = int(os.environ.get("GEMINI_MAX_OUTPUT_TOKENS", "65536"))


def resolve_model_id(requested_model: str | None = None) -> str:
    model_id = (requested_model or "").strip()
    return model_id or _DEFAULT_MODEL_ID


def build_model(model_id: str | None = None, max_tokens: int | None = None) -> Gemini:
    return Gemini(
        id=resolve_model_id(model_id),
        api_key=_GEMINI_API_KEY or None,
        temperature=0,
        top_p=1,
        seed=7,
        generation_config={"max_output_tokens": max_tokens or _MAX_OUTPUT_TOKENS},
    )


def default_dependencies() -> dict[str, Any]:
    return {
        "common_rules": common_rules,
        "policy_rules": policy_rules,
        "domain_rules": domain_rules,
    }


def default_session_state(*, ql_mode: str) -> dict[str, Any]:
    return {
        "ql_mode": ql_mode,
        "require_database": ql_mode == "pipeline",
        "connection_string": None,
        "schema": None,
        "last_reduced_findings": [],
        "_tool_calls_seen": {},
    }


def _merge_overrides(base: dict[str, Any], overrides: dict[str, Any] | None) -> dict[str, Any]:
    merged = dict(base)
    if overrides:
        merged.update(overrides)
    return merged


def _shared_agent_kwargs(
    *,
    db: SqliteDb | None,
    session_state: dict[str, Any],
    dependencies: dict[str, Any],
    playground_mode: bool,
) -> dict[str, Any]:
    pre_hooks: list[Any] = [
        prime_connection_from_prompt,
        mark_run_start,
        ensure_schema_primed,
        inject_domain_rules,
    ]
    if playground_mode:
        pre_hooks.extend([PromptInjectionGuardrail(), PIIDetectionGuardrail()])

    return {
        "db": db,
        "session_state": session_state,
        "dependencies": dependencies,
        "tool_hooks": [qualitylab_tool_hook],
        "pre_hooks": pre_hooks,
        "post_hooks": [validate_findings_entities, log_run_metrics],
        "cache_callables": False,
        "resolve_in_context": True,
    }


def build_members(
    *,
    model_id: str | None = None,
    ql_mode: str = "pipeline",
    db: SqliteDb | None = None,
    session_state: dict[str, Any] | None = None,
    dependencies: dict[str, Any] | None = None,
):
    playground_mode = ql_mode == "playground"
    shared_state = _merge_overrides(default_session_state(ql_mode=ql_mode), session_state)
    shared_dependencies = dependencies or default_dependencies()
    agent_kwargs = _shared_agent_kwargs(
        db=db,
        session_state=shared_state,
        dependencies=shared_dependencies,
        playground_mode=playground_mode,
    )

    schema_validator = Agent(
        id="schema-validator",
        tools=schema_tools,
        model=build_model(model_id),
        **SCHEMA_AGENT_CONFIG,
        **agent_kwargs,
    )
    column_validator = Agent(
        id="column-validator",
        tools=column_tools,
        model=build_model(model_id),
        **COLUMN_AGENT_CONFIG,
        **agent_kwargs,
    )
    row_validator = Agent(
        id="row-validator",
        tools=row_tools,
        model=build_model(model_id),
        **ROW_AGENT_CONFIG,
        **agent_kwargs,
    )
    crossrow_validator = Agent(
        id="crossrow-validator",
        tools=crossrow_tools,
        model=build_model(model_id),
        **CROSSROW_AGENT_CONFIG,
        **agent_kwargs,
    )
    fix_planner = Agent(
        id="fix-planner",
        tools=fixplanner_tools,
        model=build_model(model_id),
        post_hooks=[validate_findings_entities, validate_fix_sql_safety, log_run_metrics],
        **FIX_PLANNER_AGENT_CONFIG,
        **{k: v for k, v in agent_kwargs.items() if k != "post_hooks"},
    )

    return SimpleNamespace(
        schema_validator=schema_validator,
        column_validator=column_validator,
        row_validator=row_validator,
        crossrow_validator=crossrow_validator,
        fix_planner=fix_planner,
    )


def create_qualitylab_team(
    *,
    model_id: str | None = None,
    ql_mode: str = "playground",
    db: SqliteDb | None = None,
    session_state: dict[str, Any] | None = None,
    dependencies: dict[str, Any] | None = None,
):
    shared_state = _merge_overrides(default_session_state(ql_mode=ql_mode), session_state)
    shared_dependencies = dependencies or default_dependencies()
    members = build_members(
        model_id=model_id,
        ql_mode=ql_mode,
        db=db,
        session_state=shared_state,
        dependencies=shared_dependencies,
    )

    playground_mode = ql_mode == "playground"
    team_pre_hooks: list[Any] = [prime_connection_from_prompt, mark_run_start, ensure_schema_primed, inject_domain_rules]
    if playground_mode:
        team_pre_hooks.extend([PromptInjectionGuardrail(), PIIDetectionGuardrail()])

    team = Team(
        id="qualitylab-team",
        name="QualityLab Validation Team",
        mode=TeamMode.coordinate,
        model=build_model(model_id),
        members=[
            members.schema_validator,
            members.column_validator,
            members.row_validator,
            members.crossrow_validator,
            members.fix_planner,
        ],
        description=(
            "Coordinate specialist validators for PostgreSQL data-quality analysis and "
            "human-reviewable remediation planning."
        ),
        instructions=[
            "If the user provides a PostgreSQL DSN, treat it as the active connection for this session.",
            "Delegate schema-only requests to the schema specialist.",
            "Delegate column-distribution issues to the column specialist.",
            "Delegate row logic issues to the row-level specialist.",
            "Delegate cross-row and relational issues to the cross-row specialist.",
            "Delegate remediation planning to the fix planner only after findings are established.",
            "Return concise, well-structured markdown unless the user explicitly asks for raw JSON.",
        ],
        db=db,
        session_state=shared_state,
        dependencies=shared_dependencies,
        tool_hooks=[qualitylab_tool_hook],
        pre_hooks=team_pre_hooks,
        post_hooks=[log_run_metrics],
        determine_input_for_members=True,
        share_member_interactions=True,
        add_team_history_to_members=True,
        markdown=True,
        cache_callables=False,
        show_members_responses=False,
    )

    return team, members


qualitylab_team, members = create_qualitylab_team(
    model_id=None,
    ql_mode="playground",
    db=_AGENTOS_DB,
)
