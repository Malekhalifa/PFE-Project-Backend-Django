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
from agno.db.base import BaseDb
from agno.db.postgres import PostgresDb
from agno.db.sqlite import SqliteDb
from agno.guardrails.pii import PIIDetectionGuardrail
from agno.guardrails.prompt_injection import PromptInjectionGuardrail
from agno.models.google import Gemini
from google.genai.types import AutomaticFunctionCallingConfig
from agno.team import Team
from agno.team.mode import TeamMode

from .agent_config import (
    FIX_PLANNER_AGENT_CONFIG,
    PROFILER_AGENT_CONFIG,
    RELATIONAL_AGENT_CONFIG,
    SCHEMA_AGENT_CONFIG,
    STATISTICAL_AGENT_CONFIG,
    STRUCTURAL_AGENT_CONFIG,
)
from .hooks import (
    emit_pipeline_status,
    ensure_schema_primed,
    inject_domain_rules,
    log_run_metrics,
    mark_run_start,
    prime_connection_from_prompt,
    qualitylab_tool_hook,
    store_profiler_output,
    validate_findings_entities,
    validate_fix_preflight,
    validate_fix_sql_safety,
)
from .rules import common_rules, domain_rules
from .tool_factories import fixplanner_tools, profiler_tools, relational_tools, schema_tools, statistical_tools, structural_tools

_BASE_DIR = Path(__file__).resolve().parent.parent


def _make_tool_hook(agent_id: str):
    """Return a tool hook closure with the agent id pre-bound.

    In Agno workflow steps, run_context.agent resolves to the Step/Team wrapper,
    not the underlying Agent. _agent_id(run_context) therefore returns "global"
    for every agent, collapsing all per-agent dedup caches and tool-call counters
    into a single shared key. This closure bypasses that lookup entirely by
    capturing the correct agent id at construction time.
    """
    def _hook(run_context: Any, function_name: str, function_call: Any, arguments: dict) -> Any:
        return qualitylab_tool_hook(
            run_context, function_name, function_call, arguments,
            _bound_aid=agent_id,
        )
    return _hook


def _make_metrics_hook(agent_id: str, phase_name: str):
    """Return a metrics hook closure with agent id and phase name pre-bound.

    Same pattern as _make_tool_hook: in Agno workflow steps, run_context.agent
    resolves to the Step/Team wrapper — not the underlying Agent. This closure
    bypasses that lookup by capturing the correct identity at construction time,
    so Langfuse traces are recorded under the right agent name.
    """
    def _hook(run_output: Any, agent: Any, run_context: Any) -> None:
        return log_run_metrics(
            run_output, agent, run_context,
            _bound_aid=agent_id,
            _bound_phase_name=phase_name,
        )
    return _hook


def _build_agentos_db() -> BaseDb:
    pg_user = os.environ.get("PG_USER", "").strip()
    if pg_user:
        pg_pass = os.environ.get("PG_PASSWORD", "")
        pg_host = os.environ.get("PG_HOST", "localhost")
        pg_port = os.environ.get("PG_PORT", "5432")
        pg_db = os.environ.get("PG_DB", "qualitylab")
        db_url = f"postgresql://{pg_user}:{pg_pass}@{pg_host}:{pg_port}/{pg_db}"
        return PostgresDb(db_url=db_url)
    return SqliteDb(db_file=str(Path(__file__).resolve().parent / "tmp" / "agentos_sessions.db"))


_AGENTOS_DB = _build_agentos_db()
_DEFAULT_MODEL_ID = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")
_GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
_MAX_OUTPUT_TOKENS = int(os.environ.get("GEMINI_MAX_OUTPUT_TOKENS", "65536"))


def resolve_model_id(requested_model: str | None = None) -> str:
    model_id = (requested_model or "").strip()
    return model_id or _DEFAULT_MODEL_ID


_GEMINI_TIMEOUT = float(os.environ.get("GEMINI_TIMEOUT", "300"))


def build_model(model_id: str | None = None, max_tokens: int | None = None) -> Gemini:
    return Gemini(
        id=resolve_model_id(model_id),
        api_key=_GEMINI_API_KEY or None,
        temperature=0,
        top_p=1,
        seed=7,
        timeout=_GEMINI_TIMEOUT,
        generation_config={"max_output_tokens": max_tokens or _MAX_OUTPUT_TOKENS},
        # Raise the AFC cap from the SDK default of 10 to 64.
        # Must be a typed AutomaticFunctionCallingConfig — passing a plain dict is
        # silently ignored when Agno unpacks it into GenerateContentConfig(**config).
        generative_model_kwargs={
            "automatic_function_calling": AutomaticFunctionCallingConfig(maximum_remote_calls=64)
        },
    )


def default_dependencies() -> dict[str, Any]:
    return {
        "common_rules": common_rules,
        "domain_rules": domain_rules,
    }


def default_session_state(*, ql_mode: str) -> dict[str, Any]:
    return {
        "ql_mode": ql_mode,
        "require_database": ql_mode == "pipeline",
        "connection_string": None,
        "schema": None,
        "profiler_summary": {},
        "structural_profiler_summary": {},
        "profiler_schema": {},
        "validator_schema": {},
        "relational_schema": {},
        "fix_planner_schema": {},
        "schema_rules": {},
        "statistical_rules": {},
        "structural_rules": {},
        "relational_rules": {},
        "fix_planner_rules": {},
        "learned_rules": "",
        "statistical_skip_reason": "",
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
    db: BaseDb | None,
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
        "post_hooks": [validate_findings_entities],
        "cache_callables": False,
        "resolve_in_context": True,
    }


def build_members(
    *,
    model_id: str | None = None,
    ql_mode: str = "pipeline",
    db: BaseDb | None = None,
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

    if not playground_mode:
        # Inject real-time step status updates for the pipeline loading screen.
        # emit_pipeline_status reads job_id from session_state and calls
        # update_job_status so the frontend poll can advance the step indicator.
        agent_kwargs = {
            **agent_kwargs,
            "pre_hooks": list(agent_kwargs.get("pre_hooks", [])) + [emit_pipeline_status],
        }

    # Strip shared tool_hooks — each agent gets its own _make_tool_hook closure
    # so the agent id is pre-bound and not resolved from run_context.agent (which
    # returns the workflow Step wrapper in Agno workflow steps, not the Agent).
    _base_kwargs = {k: v for k, v in agent_kwargs.items() if k != "tool_hooks"}

    schema_validator = Agent(
        id="schema-validator",
        tools=schema_tools,
        model=build_model(model_id),
        tool_hooks=[_make_tool_hook("schema-validator")],
        post_hooks=list(_base_kwargs.get("post_hooks", []))
        + [_make_metrics_hook("schema-validator", "Schema Integrity Validator")],
        **SCHEMA_AGENT_CONFIG,
        **{k: v for k, v in _base_kwargs.items() if k != "post_hooks"},
    )
    # Profiler runs after schema validation and stores a statistical baseline in
    # session_state["profiler_report"] for all parallel validators to reference.
    profiler = Agent(
        id="profiler",
        tools=profiler_tools,
        model=build_model(model_id),
        tool_hooks=[_make_tool_hook("profiler")],
        post_hooks=[store_profiler_output, _make_metrics_hook("profiler", "Database Profiler")],
        **PROFILER_AGENT_CONFIG,
        **{k: v for k, v in _base_kwargs.items() if k != "post_hooks"},
    )
    statistical_validator = Agent(
        id="statistical-validator",
        tools=statistical_tools,
        model=build_model(model_id),
        tool_hooks=[_make_tool_hook("statistical-validator")],
        post_hooks=list(_base_kwargs.get("post_hooks", []))
        + [_make_metrics_hook("statistical-validator", "Statistical Validator")],
        **STATISTICAL_AGENT_CONFIG,
        **{k: v for k, v in _base_kwargs.items() if k != "post_hooks"},
    )
    structural_validator = Agent(
        id="structural-validator",
        tools=structural_tools,
        model=build_model(model_id),
        tool_hooks=[_make_tool_hook("structural-validator")],
        post_hooks=list(_base_kwargs.get("post_hooks", []))
        + [_make_metrics_hook("structural-validator", "Structural Validator")],
        **STRUCTURAL_AGENT_CONFIG,
        **{k: v for k, v in _base_kwargs.items() if k != "post_hooks"},
    )
    relational_validator = Agent(
        id="relational-validator",
        tools=relational_tools,
        model=build_model(model_id),
        tool_hooks=[_make_tool_hook("relational-validator")],
        post_hooks=list(_base_kwargs.get("post_hooks", []))
        + [_make_metrics_hook("relational-validator", "Relational Validator")],
        **RELATIONAL_AGENT_CONFIG,
        **{k: v for k, v in _base_kwargs.items() if k != "post_hooks"},
    )
    fix_planner = Agent(
        id="fix-planner",
        tools=fixplanner_tools,
        model=build_model(model_id),
        tool_hooks=[_make_tool_hook("fix-planner")],
        post_hooks=[
            validate_findings_entities,
            validate_fix_sql_safety,
            validate_fix_preflight,
            _make_metrics_hook("fix-planner", "Data Fix Planner"),
        ],
        **FIX_PLANNER_AGENT_CONFIG,
        **{k: v for k, v in _base_kwargs.items() if k != "post_hooks"},
    )

    return SimpleNamespace(
        schema_validator=schema_validator,
        profiler=profiler,
        statistical_validator=statistical_validator,
        structural_validator=structural_validator,
        relational_validator=relational_validator,
        fix_planner=fix_planner,
    )


def create_qualitylab_team(
    *,
    model_id: str | None = None,
    ql_mode: str = "playground",
    db: BaseDb | None = None,
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
            members.profiler,
            members.statistical_validator,
            members.structural_validator,
            members.relational_validator,
            members.fix_planner,
        ],
        description=(
            "Coordinate specialist validators for PostgreSQL data-quality analysis and "
            "human-reviewable remediation planning."
        ),
        instructions=[
            "If the user provides a PostgreSQL DSN, treat it as the active connection for this session.",
            "Delegate schema-only requests to the schema specialist.",
            "Delegate statistical baseline requests (null rates, cardinality, distributions) to the profiler.",
            "Delegate distribution anomalies, outliers, and drift to the statistical validator.",
            "Delegate row-level data conformance issues (impossible dates, blank required fields, negative amounts) to the structural validator.",
            "Delegate cross-row and cross-table relational issues (orphaned FKs, duplicates, functional dependencies) to the relational validator.",
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
