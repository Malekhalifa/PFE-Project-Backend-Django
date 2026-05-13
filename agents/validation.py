"""
validation.py - Workflow-backed QualityLab pipeline with legacy response adapters.
"""

from __future__ import annotations

import logging
import re
import uuid
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from agno.db.in_memory import InMemoryDb
from agno.workflow.parallel import Parallel
from agno.workflow.step import Step
from agno.workflow.types import OnError, StepOutput
from agno.workflow.workflow import Workflow

from services.report_service import save_final_pipeline_report

from .py_profiler import python_profile_step
from .reducer import _try_recover_partial, reduce_signals
from .reporting import aggregate_pipeline_scores, confidence_breakdown, derive_database_id, report_to_markdown
from .rule_family_map import build_evidence_payload, classify_fix
from .rules import common_rules
from .schemas import Finding, FixCard, FixDependencyGraph, FixPlan, ProfilerReport, ReducedFindings, ValidationReport
from .team import build_members, resolve_model_id

logger = logging.getLogger(__name__)

_FIX_ID_PATTERN = re.compile(r"[^a-z0-9]+")


def _workflow_db(job_id: str | None = None) -> InMemoryDb:  # noqa: ARG001
    return InMemoryDb()


def _slug(value: str) -> str:
    text = _FIX_ID_PATTERN.sub("_", value.lower()).strip("_")
    return text or "fix"


def _extract_custom_rules_from_text(domain_rules_text: str) -> list[str]:
    rules: list[str] = []
    for raw_line in domain_rules_text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("===") or line.startswith("---"):
            continue
        if line.startswith("-"):
            cleaned = line.lstrip("- ").strip()
            if cleaned:
                rules.append(cleaned)
    if not rules and domain_rules_text.strip():
        rules.append(domain_rules_text.strip())
    return rules


def _merge_domain_rules(raw_domain_rules: Any) -> dict[str, Any]:
    from .rules import domain_rules as load_domain_rules

    merged = dict(load_domain_rules() or {})
    merged.setdefault("custom_rules", [])

    if raw_domain_rules in (None, "", [], {}):
        return merged

    if isinstance(raw_domain_rules, dict):
        custom_rules = list(merged.get("custom_rules", []))
        incoming_custom = raw_domain_rules.get("custom_rules")
        if isinstance(incoming_custom, list):
            custom_rules.extend(str(item).strip() for item in incoming_custom if str(item).strip())
        elif incoming_custom:
            custom_rules.append(str(incoming_custom).strip())

        for key, value in raw_domain_rules.items():
            if key != "custom_rules":
                merged[key] = value
        merged["custom_rules"] = custom_rules
        return merged

    if isinstance(raw_domain_rules, list):
        merged["custom_rules"] = list(merged.get("custom_rules", []))
        for rule in raw_domain_rules:
            if isinstance(rule, dict):
                description = str(rule.get("description") or "").strip()
                if description:
                    merged["custom_rules"].append(description)
            else:
                text = str(rule).strip()
                if text:
                    merged["custom_rules"].append(text)
        return merged

    if isinstance(raw_domain_rules, str):
        merged["custom_rules"] = list(merged.get("custom_rules", []))
        merged["custom_rules"].extend(_extract_custom_rules_from_text(raw_domain_rules))
        return merged

    merged["custom_rules"] = list(merged.get("custom_rules", []))
    merged["custom_rules"].append(str(raw_domain_rules).strip())
    return merged


def _workflow_dependencies(merged_domain_rules: dict[str, Any]) -> dict[str, Any]:
    return {
        "common_rules": common_rules,
        "domain_rules": merged_domain_rules,
    }


def _initial_session_state(
    *,
    connection_string: str,
    database_id: str,
    job_id: str | None = None,
    learned_patterns: list[dict] | None = None,
) -> dict[str, Any]:
    return {
        "ql_mode": "pipeline",
        "require_database": True,
        "connection_string": connection_string,
        "database_id": database_id,
        "job_id": job_id,
        "schema": None,
        # profiler_summary is written by store_profiler_output after the profiler step.
        # Pre-initializing to {} ensures {profiler_summary} in agent instructions resolves
        # to an empty dict (not a literal string) if the profiler step fails or is skipped.
        "profiler_summary": {},
        "structural_profiler_summary": {},  # row_count only — structural agent ordering
        # Compact schema views — all populated by ensure_schema_primed.
        "profiler_schema": {},    # column name + data_type only (profiler agent)
        "validator_schema": {},   # column_details + unique_constraints (statistical + structural)
        "relational_schema": {},  # adds foreign_keys + PK fields, strips index metadata (relational agent)
        "fix_planner_schema": {}, # column_details + PK + unique_constraints, no FK/indexes (fix planner)
        # Per-agent slim domain_rules views — populated by inject_domain_rules hook.
        "schema_rules": {},
        "statistical_rules": {},
        "structural_rules": {},
        "relational_rules": {},
        "fix_planner_rules": {},
        "last_reduced_findings": [],
        # Formatted learned rules string injected into the structural agent prompt via {learned_rules}.
        # Populated by orchestrator_workflow from LearnedRule DB rows before the workflow runs.
        "learned_rules": "",
        # Set by py_profiler when no unique-pattern columns or low-cardinality columns exist.
        # Statistical agent reads {statistical_skip_reason} and returns empty report immediately.
        "statistical_skip_reason": "",
        # Legacy shared keys kept for backward compat; per-agent keys (_tool_calls_seen_<id>)
        # are the primary dedup store since Fix 1. These are no longer written by qualitylab_tool_hook.
        "_tool_calls_seen": {},
        "_query_patterns": [],  # must be a list — set is not JSON-serializable (Agno SQLite session)
        "_empty_results_streak": 0,
        "learned_patterns": learned_patterns or [],
    }


def _build_workflow_prompt(database_id: str, historical_signals: str | None) -> str:
    prompt = (
        f"Validate PostgreSQL database '{database_id}'. "
        "Use the active session connection, follow each validator's scope, and return only validated findings."
    )
    if historical_signals:
        prompt += f"\n\nHistorical signals from the previous run:\n{historical_signals.strip()}"
    return prompt


def _make_profiler_executor(session_state: dict[str, Any]):
    """Wrap python_profile_step with a closure that injects session_state values.

    Agno's step_input.workflow_session.session_data is NOT the same dict as the
    session_state passed to Workflow(...). Initial values (connection_string,
    database_id, job_id) and agent hook mutations (schema) are invisible to Step
    executors that read session_data. This closure captures the live dict reference
    and injects any missing keys before python_profile_step reads them.
    """
    def _executor(step_input):
        ws = getattr(step_input, "workflow_session", None)
        if ws is not None:
            sd = getattr(ws, "session_data", None)
            if sd is None:
                sd = {}
            # Inject keys that session_data may be missing due to the propagation gap.
            # Use session_state as the authoritative source — it is mutated in-place
            # by ensure_schema_primed, so schema/validator_schema are available here.
            for key in (
                "connection_string", "database_id", "job_id",
                "schema", "validator_schema", "relational_schema",
                "fix_planner_schema", "profiler_schema",
            ):
                if not sd.get(key) and session_state.get(key):
                    sd[key] = session_state[key]
            try:
                ws.session_data = sd
            except AttributeError:
                pass

        result = python_profile_step(step_input)

        # Sync profiler output back to the outer session_state so orchestrator_workflow
        # can read profiler_summary, profiler_report, and profiler_tables.
        # The profiler writes to ws.session_data; orchestrator_workflow reads from
        # the outer session_state — two different dict objects in this Agno version.
        if ws is not None:
            sd_after = getattr(ws, "session_data", None) or {}
            for key in ("profiler_summary", "profiler_report", "profiler_tables"):
                val = sd_after.get(key)
                if val:
                    session_state[key] = val

            # Phase-timing dict is mutated by both pre_hooks (schema_priming) and
            # post_hooks (log_run_metrics) on each agent run. Merge into outer state.
            inner_timing = sd_after.get("_phase_timing_ms")
            if isinstance(inner_timing, dict):
                outer_timing = session_state.setdefault("_phase_timing_ms", {})
                outer_timing.update(inner_timing)

        return result

    return _executor


def _make_reducer_executor(session_state: dict[str, Any]):
    """Wrap reduce_signals with the same session_data injection as the profiler.

    The reducer pushes two job-status updates (running_reducer at start,
    running_fix_planner at end) so the loading screen advances past the
    parallel-validators phase.  Both calls read job_id from
    step_input.workflow_session.session_data, which is a SEPARATE dict from
    the session_state passed to Workflow(...).  Without this wrapper, job_id
    is None inside the reducer and the status pushes silently no-op — the
    UI then sticks at 'running_reducer' (or earlier) for the full duration
    of the fix-planner LLM call (60-90s).
    """
    def _executor(step_input):
        ws = getattr(step_input, "workflow_session", None)
        if ws is not None:
            sd = getattr(ws, "session_data", None)
            if sd is None:
                sd = {}
            for key in (
                "connection_string", "database_id", "job_id",
                "schema", "validator_schema", "relational_schema",
                "fix_planner_schema", "profiler_schema",
                "profiler_report", "profiler_tables", "profiler_summary",
                # Bridge for the reducer's silent-failure audit. log_run_metrics
                # writes per-agent tool-call counts here; the audit reads them to
                # detect validators that ran tools but emitted 0 findings.
                "_validator_tool_calls",
            ):
                if not sd.get(key) and session_state.get(key):
                    sd[key] = session_state[key]
            # Merge from sd_after-style writes too: even if the agents wrote
            # _validator_tool_calls into the inner ws.session_data on their own,
            # union the dicts so neither side wins by overwrite.
            inner_counts = sd.get("_validator_tool_calls") or {}
            outer_counts = session_state.get("_validator_tool_calls") or {}
            if outer_counts or inner_counts:
                merged = {**outer_counts, **inner_counts}
                sd["_validator_tool_calls"] = merged
            try:
                ws.session_data = sd
            except AttributeError:
                pass

        result = reduce_signals(step_input)

        # Sync reducer-produced state back so the orchestrator can read it.
        if ws is not None:
            sd_after = getattr(ws, "session_data", None) or {}
            reduced = sd_after.get("last_reduced_findings")
            if reduced:
                session_state["last_reduced_findings"] = reduced
            inner_timing = sd_after.get("_phase_timing_ms")
            if isinstance(inner_timing, dict):
                outer_timing = session_state.setdefault("_phase_timing_ms", {})
                outer_timing.update(inner_timing)
            inner_counts = sd_after.get("_validator_tool_calls")
            if isinstance(inner_counts, dict):
                outer_counts = session_state.setdefault("_validator_tool_calls", {})
                outer_counts.update(inner_counts)

        return result

    return _executor


def build_validation_workflow(
    *,
    resolved_model_id: str,
    session_state: dict[str, Any],
    dependencies: dict[str, Any],
    job_id: str | None = None,
) -> tuple[Workflow, Any]:
    db = _workflow_db(job_id)
    members = build_members(
        model_id=resolved_model_id,
        ql_mode="pipeline",
        db=db,
        session_state=session_state,
        dependencies=dependencies,
    )
    workflow = Workflow(
        name="QualityLab Validation",
        db=db,
        session_state=session_state,
        dependencies=dependencies,
        steps=[
            Step(name="schema",   agent=members.schema_validator),
            # Pure-Python profiler: deterministic SQL, concurrent table scanning,
            # no LLM overhead, ~10-15× faster than the LLM profiler agent.
            # Wrapped in _make_profiler_executor to bridge Agno's session_data
            # propagation gap (step executors don't see agent pre_hook mutations).
            Step(name="profiler", executor=_make_profiler_executor(session_state)),
            Parallel(
                Step(name="statistical", agent=members.statistical_validator, on_error=OnError.fail),
                Step(name="structural",  agent=members.structural_validator,  on_error=OnError.fail),
                Step(name="relational",  agent=members.relational_validator,  on_error=OnError.fail),
                name="Validation Phase",
            ),
            # Reducer is also a Step executor with the same session_data
            # propagation gap as the profiler — wrap it so its status pushes
            # (running_reducer, running_fix_planner) actually fire.
            Step(name="reduce",   executor=_make_reducer_executor(session_state)),
            Step(name="fix_plan", agent=members.fix_planner),
        ],
    )
    return workflow, members


_MAX_RELATIONAL_RETRIES = 2
_RELATIONAL_RETRY_DELAY_S = 5

_TRANSIENT_MARKERS = ("504", "DEADLINE_EXCEEDED", "timed out", "timeout", "UNAVAILABLE", "RESOURCE_EXHAUSTED")


def _is_transient_error(content: Any) -> bool:
    if not isinstance(content, str):
        return False
    lower = content.lower()
    return any(m.lower() in lower for m in _TRANSIENT_MARKERS)


def _run_relational_standalone(
    *,
    relational_validator: Any,
    session_state: dict[str, Any],
    database_id: str,
) -> ValidationReport:
    import time as _time
    for attempt in range(1, _MAX_RELATIONAL_RETRIES + 1):
        logger.warning("Relational agent transient failure — retry %d/%d in %ds",
                       attempt, _MAX_RELATIONAL_RETRIES, _RELATIONAL_RETRY_DELAY_S)
        _time.sleep(_RELATIONAL_RETRY_DELAY_S)
        try:
            response = relational_validator.run(
                input=(
                    f"Re-run relational analysis for database '{database_id}'. "
                    "The previous attempt timed out. Execute Phase 1 and Phase 2 fully."
                ),
                session_state=session_state,
            )
            report = _as_validation_report(getattr(response, "content", ""), "relational")
            if report.findings:
                return report
        except Exception as exc:
            logger.error("Relational retry %d/%d failed: %s", attempt, _MAX_RELATIONAL_RETRIES, exc)
    return ValidationReport(validator="relational", findings=[])


_MAX_PROFILER_RETRIES = 2
_PROFILER_RETRY_DELAY_S = 5


def _run_profiler_standalone(
    *,
    profiler: Any,
    session_state: dict[str, Any],
    database_id: str,
) -> Any:
    """Retry the profiler agent directly when the workflow step fails silently.

    On success, writes profiler_summary and profiler_report into session_state so
    downstream statistical/structural validators have the baseline they need.
    """
    import time as _time

    for attempt in range(1, _MAX_PROFILER_RETRIES + 1):
        logger.warning(
            "Profiler recovery — standalone retry %d/%d in %ds",
            attempt, _MAX_PROFILER_RETRIES, _PROFILER_RETRY_DELAY_S,
        )
        _time.sleep(_PROFILER_RETRY_DELAY_S)
        try:
            response = profiler.run(
                input=f"Profile all tables in database '{database_id}'.",
                session_state=session_state,
            )
            content = getattr(response, "content", None)
            report = _as_profiler_report(content)
            if report.tables:
                # Mirror what store_profiler_output normally does via the post_hook.
                session_state["profiler_report"] = report.model_dump()
                session_state["profiler_summary"] = {
                    t.table_name: {
                        "row_count": t.row_count,
                        "columns": {
                            c.column_name: {
                                "null_rate":    c.null_rate,
                                "outlier_flag": c.outlier_flag,
                                "cardinality":  c.cardinality,
                                "total_rows":   c.total_rows,
                            }
                            for c in t.column_stats
                        },
                    }
                    for t in report.tables
                }
                logger.info(
                    "Profiler recovery succeeded — profiled %d tables on retry %d",
                    len(report.tables), attempt,
                )
                return report
        except Exception as exc:
            logger.error(
                "Profiler standalone retry %d/%d failed: %s",
                attempt, _MAX_PROFILER_RETRIES, exc,
            )

    return _as_profiler_report(None)


def _rerun_statistical_structural(
    *,
    members: Any,
    session_state: dict[str, Any],
    database_id: str,
    skip_statistical: bool = False,
) -> tuple[Any, Any]:
    """Re-run statistical and structural validators after a successful profiler recovery.

    Returns a (statistical_report, structural_report) tuple. Falls back to an empty
    ValidationReport on any error so the overall pipeline can still complete.
    skip_statistical skips the statistical agent when profiler coverage is too low.
    """
    results: dict[str, Any] = {}
    agents_to_run = []
    if not skip_statistical:
        agents_to_run.append(("statistical", members.statistical_validator))
    agents_to_run.append(("structural", members.structural_validator))
    for name, agent in agents_to_run:
        try:
            response = agent.run(
                input=(
                    f"Re-run {name} analysis for database '{database_id}'. "
                    "Profiler baseline is now available in session state."
                ),
                session_state=session_state,
            )
            results[name] = _as_validation_report(
                getattr(response, "content", ""), name
            )
            logger.info(
                "%s re-run after profiler recovery: %d finding(s)",
                name, len(results[name].findings),
            )
        except Exception as exc:
            logger.error("%s re-run after profiler recovery failed: %s", name, exc)
            results[name] = ValidationReport(validator=name, findings=[])

    return (
        results.get("statistical", ValidationReport(validator="statistical", findings=[])),
        results.get("structural",  ValidationReport(validator="structural",  findings=[])),
    )


class _SyntheticWorkflowSession:
    """Minimal stub so ReduceSignals can read/write session_data outside a workflow."""

    def __init__(self, session_data: dict[str, Any]) -> None:
        self.session_data = session_data


class _SyntheticStepInput:
    """Minimal stub so ReduceSignals can call get_step_content() outside a workflow."""

    def __init__(
        self,
        reports: dict[str, Any],
        session_state: dict[str, Any],
    ) -> None:
        self._reports = reports
        self.workflow_session = _SyntheticWorkflowSession(session_state)

    def get_step_content(self, step_name: str) -> Any:
        return self._reports.get(step_name)


def _rerun_fix_planner(
    *,
    fix_planner: Any,
    reduced_findings: Any,
    session_state: dict[str, Any],
    database_id: str,
) -> Any:
    """Re-run the fix planner after profiler recovery expands the finding set."""
    try:
        fdg_json = (
            reduced_findings.model_dump_json()
            if hasattr(reduced_findings, "model_dump_json")
            else json.dumps(reduced_findings)
        )
        response = fix_planner.run(
            input=(
                f"Generate a fix plan for database '{database_id}' "
                f"based on these findings:\n{fdg_json}"
            ),
            session_state=session_state,
        )
        return _as_fix_plan(getattr(response, "content", ""))
    except Exception as exc:
        logger.error("Fix planner re-run after profiler recovery failed: %s", exc)
        return None


def _collect_step_outputs(step_results: list[Any]) -> dict[str, StepOutput]:
    outputs: dict[str, StepOutput] = {}

    def visit(item: Any) -> None:
        if item is None:
            return
        if isinstance(item, list):
            for nested in item:
                visit(nested)
            return
        step_name = getattr(item, "step_name", None)
        if step_name:
            outputs[step_name] = item
        for nested in getattr(item, "steps", []) or []:
            visit(nested)

    for step in step_results or []:
        visit(step)
    return outputs


_STEP_TO_PHASE: dict[str, str] = {
    "schema":      "Schema Integrity Validator",
    "statistical": "Statistical Validator",
    "structural":  "Structural Validator",
    "relational":  "Relational Validator",
    "fix_plan":    "Data Fix Planner",
}


def _update_phase_timing_from_steps(
    step_outputs: dict[str, Any],
    session_state: dict[str, Any],
) -> None:
    """Overwrite _phase_timing_ms entries for agent steps using Agno's own RunMetrics.

    RunContext.agent is not available in post_hooks for workflow steps, so log_run_metrics
    cannot identify which phase fired and writes all entries under the generic key "team".
    This function reads step_output.metrics.duration (seconds, set by Agno's agent runner)
    and writes correctly-named entries, replacing the "team" overwrites.
    """
    timing = session_state.setdefault("_phase_timing_ms", {})
    for step_name, step_output in step_outputs.items():
        phase = _STEP_TO_PHASE.get(step_name)
        if phase is None:
            continue
        metrics = getattr(step_output, "metrics", None)
        duration_s = getattr(metrics, "duration", None)
        if duration_s is not None:
            timing[phase] = round(duration_s * 1000)


def _as_validation_report(value: Any, fallback_validator: str = "unknown") -> ValidationReport:
    if isinstance(value, ValidationReport):
        return value
    if isinstance(value, dict):
        return ValidationReport.model_validate(value)
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            return ValidationReport.model_validate(parsed)
        except Exception:
            recovered = _try_recover_partial(value, fallback_validator)
            if recovered is not None:
                return recovered
            logger.warning(
                "Failed to parse ValidationReport from string, falling back to empty. Content: %s",
                value[:200],
            )
            return ValidationReport(validator=fallback_validator, findings=[])
    logger.warning("Expected ValidationReport but got %s, falling back to empty.", type(value).__name__)
    return ValidationReport(validator=fallback_validator, findings=[])


def _as_profiler_report(value: Any) -> ProfilerReport:
    if isinstance(value, ProfilerReport):
        return value
    if isinstance(value, dict):
        try:
            return ProfilerReport.model_validate(value)
        except Exception:
            pass
    return ProfilerReport(tables=[])


def _as_reduced_findings(value: Any) -> ReducedFindings:
    if isinstance(value, ReducedFindings):
        return value
    if isinstance(value, FixDependencyGraph):
        return value.to_reduced_findings()
    if isinstance(value, dict):
        # Try FixDependencyGraph first (it's a superset of ReducedFindings)
        try:
            return FixDependencyGraph.model_validate(value).to_reduced_findings()
        except Exception:
            pass
        return ReducedFindings.model_validate(value)
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            return _as_reduced_findings(parsed)
        except Exception:
            logger.warning("Failed to parse ReducedFindings from string, falling back to empty. Content: %s", value[:100])
            return ReducedFindings(findings=[])
    logger.warning("Expected ReducedFindings but got %s, falling back to empty.", type(value).__name__)
    return ReducedFindings(findings=[])


def _as_fix_plan(value: Any) -> FixPlan:
    if isinstance(value, FixPlan):
        return value
    if isinstance(value, dict):
        return FixPlan.model_validate(value)
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            return FixPlan.model_validate(parsed)
        except Exception:
            logger.warning("Failed to parse FixPlan from string, falling back to empty. Content: %s", value[:100])
            return FixPlan(cards=[])
    logger.warning("Expected FixPlan but got %s, falling back to empty.", type(value).__name__)
    return FixPlan(cards=[])


def _split_entity(entity: str) -> tuple[str, str | None]:
    if "." in entity:
        table, column = entity.split(".", 1)
        return table, column
    return entity, None


def _severity_summary(findings: list[Finding]) -> dict[str, int]:
    counts = {"critical": 0, "high": 0, "medium": 0, "low": 0}
    for finding in findings:
        counts[finding.severity] = counts.get(finding.severity, 0) + 1
    return counts


# Map duplicate rule names to a canonical rule. Different code paths (LLM agents
# vs deterministic py_profiler) emit the same logical issue under different names.
# Canonicalize before dedup so each issue counts once.
_RULE_ALIASES: dict[str, str] = {
    # Schema: missing primary key
    "primary_key_required": "missing_pk",
    "missing_pk": "missing_pk",
    # Schema: nullable foreign-key column
    "nullable_foreign_key": "nullable_fk",
    "nullable_fk_column": "nullable_fk",
    "nullable_fk": "nullable_fk",
    # Schema: foreign-key column without an index
    "missing_index": "missing_fk_index",
    "missing_index_fk_column": "missing_fk_index",
    "missing_fk_index": "missing_fk_index",
    # Statistical: high null rate
    "null_rate_high": "null_rate_high",
    "null_ratio_high": "null_rate_high",
    # Structural: cross-column logical constraint violations (driven by domain_rules.custom_rules)
    "custom_rule_violation": "custom_rule_violation",
    "business_rule_violation": "custom_rule_violation",
    "logical_inconsistency": "custom_rule_violation",
}


def _canonical_rule(rule: str | None) -> str:
    if not rule:
        return ""
    return _RULE_ALIASES.get(rule, rule)


def _dedup_findings_by_alias(findings: list[Finding]) -> list[Finding]:
    """Collapse findings whose rule maps to the same canonical name on the same entity.

    Keeps the highest-severity finding, averages confidence, rewrites the rule
    field to the canonical name. Drops the duplicate copy entirely.
    """
    if not findings:
        return findings

    sev_rank = {"low": 1, "medium": 2, "high": 3, "critical": 4}
    groups: dict[tuple[str, str], list[Finding]] = {}
    order: list[tuple[str, str]] = []
    for f in findings:
        key = (f.entity or "", _canonical_rule(f.rule))
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append(f)

    deduped: list[Finding] = []
    for key in order:
        bucket = groups[key]
        if len(bucket) == 1:
            f = bucket[0]
            canon = _canonical_rule(f.rule)
            if canon and canon != f.rule:
                f = f.model_copy(update={"rule": canon})
            deduped.append(f)
            continue

        best = max(bucket, key=lambda f: sev_rank.get(f.severity, 0))
        confidences = [f.confidence for f in bucket if isinstance(f.confidence, (int, float))]
        avg_confidence = round(sum(confidences) / len(confidences), 4) if confidences else best.confidence
        deduped.append(best.model_copy(update={
            "rule": _canonical_rule(best.rule),
            "confidence": avg_confidence,
        }))
    return deduped


def _quality_score(findings: list[Finding], total_tables: int) -> float:
    """
    Severity-weighted score normalized by table count.

    weights:    critical=18, high=7, medium=1.5, low=0.1
    denominator: max(1, total_tables // 5)   — 30-table schema → 6
    score      = max(0, 100 - min(100, total_penalty / denominator))

    Calibration: 4 critical PK violations alone on a 30-table schema score ~88
    (4*18=72, 72/6=12 → 88). Pre-dedup bloat no longer hides serious issues.
    """
    weights = {"critical": 18.0, "high": 7.0, "medium": 1.5, "low": 0.1}
    penalty = sum(weights.get(f.severity, 0) for f in findings)
    denom = max(1.0, total_tables / 5.0)
    normalized = penalty / denom
    return round(max(0.0, 100.0 - min(100.0, normalized)), 1)


def _legacy_validation_finding(finding: Finding) -> dict[str, Any]:
    table_name, column_name = _split_entity(finding.entity)
    return {
        "table": table_name,
        "column": column_name,
        "entity": finding.entity,
        "rule": finding.rule,
        "severity": finding.severity,
        "confidence": finding.confidence,
        "evidence": finding.evidence,
        "remediation_hint": finding.suggested_action,
    }


def _group_findings_by_table_rule(findings: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """
    Roll up multiple (table, rule) findings into one entry with affected_columns.
    Cuts row count when same rule fires across many columns of the same table.
    """
    from collections import OrderedDict

    groups: "OrderedDict[tuple[str, str], list[dict[str, Any]]]" = OrderedDict()
    for f in findings:
        key = (f.get("table") or "", f.get("rule") or "")
        groups.setdefault(key, []).append(f)

    rolled: list[dict[str, Any]] = []
    for (_table, _rule), group in groups.items():
        if len(group) == 1:
            rolled.append(group[0])
            continue

        cols: list[str] = []
        seen: set[str] = set()
        for f in group:
            c = f.get("column")
            if c and c not in seen:
                seen.add(c)
                cols.append(c)

        sev_rank = {"low": 1, "medium": 2, "high": 3, "critical": 4}
        best = max(group, key=lambda f: sev_rank.get(f.get("severity", "low"), 0))
        confidences = [f["confidence"] for f in group if isinstance(f.get("confidence"), (int, float))]
        avg_confidence = round(sum(confidences) / len(confidences), 4) if confidences else best.get("confidence")

        merged = dict(best)
        merged["column"] = None
        merged["affected_columns"] = cols
        merged["count"] = len(group)
        merged["confidence"] = avg_confidence
        rolled.append(merged)

    return rolled


def _legacy_validation_report(
    *,
    report: ValidationReport,
    agent_name: str,
    pipeline_run_id: str,
    database_id: str,
    run_timestamp: str,
    total_entities: int | None = None,
) -> dict[str, Any]:
    deduped_findings = _dedup_findings_by_alias(report.findings)
    raw_findings = [_legacy_validation_finding(item) for item in deduped_findings]
    findings = _group_findings_by_table_rule(raw_findings)

    severity = _severity_summary(deduped_findings)
    flagged_tables = sorted({item["table"] for item in findings if item.get("table")})
    avg_confidence = (
        round(sum(item.confidence for item in deduped_findings) / len(deduped_findings), 4)
        if deduped_findings
        else 1.0
    )
    items_total = total_entities if total_entities is not None else len(deduped_findings)
    quality_score = _quality_score(deduped_findings, items_total)
    items_flagged = len(findings)
    items_passed = max(0, items_total - items_flagged)

    return {
        "pipeline_run_id": pipeline_run_id,
        "database_id": database_id,
        "run_timestamp": run_timestamp,
        "agent": agent_name,
        "status": "completed",
        "summary": {
            "total_items_analyzed": items_total,
            "items_passed": items_passed,
            "items_flagged": items_flagged,
            "critical_issues": severity["critical"],
            "high_issues": severity["high"],
            "medium_issues": severity["medium"],
            "low_issues": severity["low"],
            "overall_quality_score": quality_score,
            "overall_confidence": avg_confidence,
            "flagged_tables": flagged_tables,
        },
        "metrics": {
            "validator": report.validator,
            "findings_count": len(report.findings),
            "deduped_finding_count": len(deduped_findings),
            "grouped_finding_count": len(findings),
        },
        "findings": findings,
    }


def _split_sql_statements(sql_text: str) -> list[str]:
    statements = [statement.strip() for statement in sql_text.split(";")]
    return [f"{statement};" for statement in statements if statement]


def _match_finding_for_card(card: FixCard, reduced_findings: ReducedFindings) -> Finding | None:
    for finding in reduced_findings.findings:
        if finding.entity == card.target:
            return finding
        table_name, _ = _split_entity(finding.entity)
        if card.target == table_name:
            return finding
    return None


def _legacy_fix_entry(card: FixCard, index: int, reduced_findings: ReducedFindings) -> dict[str, Any]:
    matched = _match_finding_for_card(card, reduced_findings)
    table_name, _ = _split_entity(card.target)
    queries = _split_sql_statements(card.sql)
    source_rule = card.source_rule or (matched.rule if matched else "")
    if not source_rule:
        logger.warning(
            "FixCard '%s' empty source_rule → classification fallback to unknown. "
            "target=%s card_family=%s",
            card.fix_id or card.title, card.target, card.card_family,
        )
    severity = matched.severity if matched else card.severity
    confidence = matched.confidence if matched else card.confidence
    evidence = dict(card.evidence or (matched.evidence if matched else {}))
    if card.card_family != "unknown" or not matched:
        family = card.card_family
        interaction_model = card.interaction_model
        source_type = card.source_type
    else:
        family, interaction_model, source_type = classify_fix(source_rule, matched.validator)
    evidence_payload = card.evidence_payload or build_evidence_payload(
        family=family,
        rule=source_rule,
        target=card.target,
        evidence=evidence,
    )
    return {
        "id": f"fix_{index:03d}_{_slug(card.target)}",
        # Phase 6 fields — dependency-aware fix review
        "fix_id": card.fix_id or f"fix_{index:03d}_{_slug(card.target)}",
        "rows_affected_estimate": card.rows_affected_estimate,
        "tables_touched": list(card.tables_touched),
        "depends_on": list(card.depends_on),
        "table": table_name,
        "target_table": table_name,
        "problem": card.title,
        "fix": card.rationale,
        "severity": severity,
        "confidence": confidence,
        "risk_level": card.risk_level,
        "card_family": family,
        "interaction_model": interaction_model,
        "source_rule": source_rule,
        "source_type": source_type,
        "evidence": evidence,
        "evidence_payload": evidence_payload,
        "estimated_rows_affected": card.rows_affected_estimate or 0,
        "queries": queries,
        "explanation": card.rationale,
        "preconditions": list(card.preconditions),
        "verification_queries": list(card.verification_queries),
        "rollback_queries": list(card.rollback_queries),
        "explain_summary": card.explain_summary,
    }


def _legacy_fix_report(
    *,
    fix_plan: FixPlan,
    reduced_findings: ReducedFindings,
    pipeline_run_id: str,
    database_id: str,
    run_timestamp: str,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    fixes = [
        _legacy_fix_entry(card, index=index, reduced_findings=reduced_findings)
        for index, card in enumerate(fix_plan.cards, start=1)
    ]
    avg_confidence = (
        round(sum(item["confidence"] for item in fixes) / len(fixes), 4)
        if fixes
        else 1.0
    )
    # Severity-weighted, normalized by table count — matches validator formula.
    weights = {"critical": 10.0, "high": 5.0, "medium": 1.0, "low": 0.1}
    flagged_tables_list = sorted({item["table"] for item in fixes if item.get("table")})
    penalty = sum(weights.get(f.get("severity", "low"), 0) for f in fixes)
    quality_score = round(
        max(0.0, 100.0 - min(100.0, penalty / max(1, len(flagged_tables_list)))),
        1,
    )
    report = {
        "pipeline_run_id": pipeline_run_id,
        "database_id": database_id,
        "run_timestamp": run_timestamp,
        "agent": "Data Fix Planner",
        "status": "completed",
        "summary": {
            "total_items_analyzed": len(reduced_findings.findings),
            "items_passed": max(0, len(reduced_findings.findings) - len(fixes)),
            "items_flagged": len(fixes),
            "critical_issues": len([f for f in fixes if f["severity"] == "critical"]),
            "high_issues": len([f for f in fixes if f["severity"] == "high"]),
            "medium_issues": len([f for f in fixes if f["severity"] == "medium"]),
            "low_issues": len([f for f in fixes if f["severity"] == "low"]),
            "overall_quality_score": quality_score,
            "overall_confidence": avg_confidence,
            "flagged_tables": flagged_tables_list,
        },
        "metrics": {
            "total_issues_received": len(reduced_findings.findings),
            "fixes_generated": len(fixes),
        },
        "findings": fixes,
        "cards": [card.model_dump(mode="json") for card in fix_plan.cards],
    }
    return report, fixes


def _build_executive_summary(
    *,
    schema_dict: dict[str, Any],
    statistical_dict: dict[str, Any],
    structural_dict: dict[str, Any],
    relational_dict: dict[str, Any],
    fix_dict: dict[str, Any],
    data_quality_score: int,
    total_tables: int,
    profiler_table_count: int,
) -> dict[str, Any]:
    """Top-level verdict, top issues, scan stats — surfaces signal above the 200+ findings."""
    all_reports = [schema_dict, statistical_dict, structural_dict, relational_dict]
    sev_rank = {"critical": 4, "high": 3, "medium": 2, "low": 1}

    flat: list[dict[str, Any]] = []
    for r in all_reports:
        for f in r.get("findings", []) or []:
            if isinstance(f, dict):
                flat.append(f)

    flat.sort(
        key=lambda f: (sev_rank.get(f.get("severity", "low"), 0), f.get("confidence") or 0),
        reverse=True,
    )

    top_issues: list[str] = []
    for f in flat[:3]:
        sev = (f.get("severity") or "?").upper()
        rule = (f.get("rule") or "issue").replace("_", " ")
        entity = f.get("entity") or f.get("table") or "?"
        cols = f.get("affected_columns") or []
        suffix = f" ({len(cols)} columns)" if cols else ""
        top_issues.append(f"[{sev}] {rule} on {entity}{suffix}")

    if data_quality_score >= 80:
        verdict = "low_risk"
    elif data_quality_score >= 50:
        verdict = "medium_risk"
    elif data_quality_score >= 25:
        verdict = "high_risk"
    else:
        verdict = "critical"

    fix_findings = fix_dict.get("findings", []) or []
    cards = fix_dict.get("cards", []) or []

    # ── Migration risk scoring ────────────────────────────────────────────
    # The data_quality_score reflects diagnosis quality only. The verdict the
    # user sees should also reflect remediation safety: a clean diagnosis
    # paired with destructive fix cards (DELETE, placeholder FK fills, blocked
    # cards) is NOT low risk in practice. Demote the verdict accordingly.
    high_risk_cards = sum(
        1 for c in cards
        if isinstance(c, dict) and (c.get("risk_level") or "").lower() == "high"
    )
    blocked_cards = sum(
        1 for c in cards
        if isinstance(c, dict)
        and (
            (c.get("interaction_model") or "").lower() == "blocked"
            or "BLOCKED BY PREFLIGHT" in (c.get("sql") or "")
        )
    )
    destructive_cards = sum(
        1 for c in cards
        if isinstance(c, dict)
        and any(
            kw in (c.get("sql") or "").upper()
            for kw in ("DELETE FROM", "DROP TABLE", "DROP CONSTRAINT", "TRUNCATE")
        )
    )
    migration_risk_score = (
        high_risk_cards * 10
        + blocked_cards * 15
        + destructive_cards * 8
    )

    _verdict_rank = {"low_risk": 1, "medium_risk": 2, "high_risk": 3, "critical": 4}
    _rank_to_verdict = {v: k for k, v in _verdict_rank.items()}

    # Demotion rules — bump verdict severity by N levels based on migration_risk_score.
    base_rank = _verdict_rank.get(verdict, 1)
    if migration_risk_score >= 30:
        adjusted_rank = max(base_rank, 3)  # at least high_risk
    elif migration_risk_score >= 15:
        adjusted_rank = max(base_rank, 2)  # at least medium_risk
    elif blocked_cards > 0:
        adjusted_rank = max(base_rank, 2)  # any blocked card → at least medium
    else:
        adjusted_rank = base_rank
    verdict = _rank_to_verdict.get(adjusted_rank, verdict)

    return {
        "verdict": verdict,
        "top_issues": top_issues or ["No issues detected."],
        "tables_scanned": total_tables,
        "tables_profiled": profiler_table_count,
        "fixes_recommended": len(fix_findings),
        "critical_fix_count": sum(1 for x in fix_findings if x.get("severity") == "critical"),
        "migration_risk": {
            "score": migration_risk_score,
            "high_risk_cards": high_risk_cards,
            "blocked_cards": blocked_cards,
            "destructive_cards": destructive_cards,
        },
    }



# ──────────────────────────────────────────────────────────────────────────
# Orchestrator decomposition — typed state containers + focused helpers.
#
# Mutation contract:
#   • In-place mutators end with `_into` and return None.
#   • Replacers return the new value (caller assigns).
#   • The recovery helper mutates several fields of a shared dataclass in
#     place because it conditionally replaces 5 different reports — packing
#     all those returns would be more error-prone than the dataclass write.
# ──────────────────────────────────────────────────────────────────────────


@dataclass
class _InitialReports:
    """Container for the 7 pipeline reports.

    Mutation rules:
      • schema and statistical have their .findings list appended to by the
        auto-merge helpers (in-place mutation of the report objects).
      • profiler / statistical / structural / relational / reduced / fix_plan
        may be wholesale REPLACED by the relational retry and recovery paths.
      • schema is never replaced; relational is never replaced by the recovery
        path (only by the transient retry).
    """
    schema: ValidationReport
    profiler: ProfilerReport
    statistical: ValidationReport
    structural: ValidationReport
    relational: ValidationReport
    reduced: ReducedFindings
    fix_plan: FixPlan


@dataclass
class _LegacyReports:
    """Dict-shaped outputs consumed by the frontend / saved bundle."""
    schema: dict[str, Any]
    statistical: dict[str, Any]
    structural: dict[str, Any]
    relational: dict[str, Any]
    fix: dict[str, Any]
    legacy_fixes: list[dict[str, Any]]


@dataclass
class _SummaryArtifacts:
    """Computed summary-layer outputs."""
    confidence_score: int
    data_quality_score: int
    confidence_info: dict[str, Any]
    exec_summary: dict[str, Any]
    timing_info: dict[str, Any]


def _parse_initial_reports(step_outputs: dict[str, Any]) -> _InitialReports:
    """Pure parse: convert each step output into its typed report.

    Identical to the inline parsing in the legacy orchestrator — only
    difference is that ALL parses are grouped here. _as_* parsers are
    pure (no side effects), so call order is irrelevant.
    """
    return _InitialReports(
        schema=_as_validation_report(
            getattr(step_outputs.get("schema"), "content", ""), "schema"
        ),
        profiler=_as_profiler_report(
            getattr(step_outputs.get("profiler"), "content", "")
        ),
        statistical=_as_validation_report(
            getattr(step_outputs.get("statistical"), "content", ""), "statistical"
        ),
        structural=_as_validation_report(
            getattr(step_outputs.get("structural"), "content", ""), "structural"
        ),
        relational=_as_validation_report(
            getattr(step_outputs.get("relational"), "content", ""), "relational"
        ),
        reduced=_as_reduced_findings(
            getattr(step_outputs.get("reduce"), "content", "")
        ),
        fix_plan=_as_fix_plan(
            getattr(step_outputs.get("fix_plan"), "content", "")
        ),
    )


def _merge_schema_auto_findings_into(
    schema_report: ValidationReport,
    session_state: dict[str, Any],
) -> None:
    """Inject deterministic schema findings post-LLM (in-place).

    Reads session_state['schema_auto_findings'] (populated by the schema
    priming hook) and appends any (entity, rule) pairs not already present in
    schema_report.findings. Logs a consistency warning when LLM coverage
    falls below 50% of the deterministic baseline.

    Mutates: schema_report.findings (list append).
    Returns: None.
    """
    auto = session_state.get("schema_auto_findings") or []
    llm_count = len(schema_report.findings)
    if not auto:
        return
    existing = {(f.entity, f.rule) for f in schema_report.findings}
    merged = 0
    for raw in auto:
        try:
            sf = Finding.model_validate(raw)
            if (sf.entity, sf.rule) not in existing:
                schema_report.findings.append(sf)
                existing.add((sf.entity, sf.rule))
                merged += 1
        except Exception:
            pass
    if merged:
        logger.info(
            "Merged %d deterministic schema finding(s) into schema report "
            "(LLM agent found %d, schema snapshot detected %d)",
            merged, llm_count, len(auto),
        )
    if llm_count < len(auto) * 0.50:
        logger.warning(
            "CONSISTENCY CHECK: LLM schema agent found only %d finding(s) vs "
            "%d from deterministic schema snapshot — possible under-detection.",
            llm_count, len(auto),
        )


def _maybe_retry_relational(
    relational_report: ValidationReport,
    raw_relational_content: Any,
    *,
    members: Any,
    session_state: dict[str, Any],
    database_id: str,
) -> ValidationReport:
    """Retry the relational agent when the initial run was a transient error.

    Trigger: relational_report has zero findings AND the raw step content
    matches a transient error marker (504, DEADLINE_EXCEEDED, timeout, etc.).
    Step.max_retries cannot catch this because Agno surfaces the error as
    string content rather than an exception.

    Returns: the new ValidationReport on retry success, otherwise the
    original report unchanged.
    """
    if relational_report.findings:
        return relational_report
    if not _is_transient_error(raw_relational_content):
        return relational_report
    return _run_relational_standalone(
        relational_validator=members.relational_validator,
        session_state=session_state,
        database_id=database_id,
    )


def _merge_profiler_auto_findings_into(
    statistical_report: ValidationReport,
    session_state: dict[str, Any],
) -> None:
    """Inject deterministic profiler findings into the statistical report.

    Reads session_state['profiler_auto_findings'] (populated by py_profiler's
    deterministic null-rate detector) and appends any new (entity, rule)
    pairs to statistical_report.findings. Logs a consistency warning on LLM
    under-detection (LLM emitted < 50% of deterministic count).

    Mutates: statistical_report.findings (list append).
    Returns: None.
    """
    auto = session_state.get("profiler_auto_findings") or []
    llm_count = len(statistical_report.findings)
    if not auto:
        return
    existing = {(f.entity, f.rule) for f in statistical_report.findings}
    merged = 0
    for raw in auto:
        try:
            f = Finding.model_validate(raw)
            if (f.entity, f.rule) not in existing:
                statistical_report.findings.append(f)
                existing.add((f.entity, f.rule))
                merged += 1
        except Exception:
            pass
    if merged:
        logger.info(
            "Merged %d deterministic profiler finding(s) into statistical report "
            "(LLM agent found %d, profiler detected %d)",
            merged, llm_count, len(auto),
        )
    if llm_count < len(auto) * 0.50:
        logger.warning(
            "CONSISTENCY CHECK: LLM statistical agent found only %d finding(s) vs "
            "%d from deterministic profiler — possible under-detection. "
            "Auto-merged findings are included in the final report.",
            llm_count, len(auto),
        )


def _run_recovery_path_if_needed(
    reports: _InitialReports,
    *,
    members: Any,
    session_state: dict[str, Any],
    database_id: str,
) -> None:
    """Recover from a silent profiler-step failure (e.g. MALFORMED_FUNCTION_CALL).

    Trigger: session_state['profiler_summary'] is empty/missing after the
    workflow run — meaning the profiler post_hook never fired (Agno bypasses
    post_hooks when the agent returns a malformed function call).

    Recovery sequence (each step gated on prior success):
      1. Re-run the profiler agent standalone → reports.profiler replaced.
         If that produces no tables, log error and return early.
      2. Compute coverage_ratio. If known_tables > 0 and coverage < 0.5,
         skip the statistical re-run (too few tables profiled to ground
         the LLM — likely-hallucinated output). Structural always re-runs.
      3. Re-run statistical and structural validators against the recovered
         baseline → reports.statistical, reports.structural replaced.
      4. Re-run the deterministic reducer over all four validator reports
         using a SyntheticStepInput → reports.reduced replaced.
      5. Re-run the fix planner — only if the new reducer produced findings —
         passing the raw FixDependencyGraph (NOT the parsed ReducedFindings)
         so model_dump_json() works on the agent input. reports.fix_plan
         replaced when the planner returned a result.

    Mutates: reports.{profiler, statistical, structural, reduced, fix_plan}
             in-place. reports.schema and reports.relational are never touched.
    Returns: None.

    Note: the auto-findings merge that ran on reports.statistical BEFORE this
    helper is discarded when statistical is replaced here. This matches legacy
    behavior — preserved as-is, NOT a bug being fixed.
    """
    if session_state.get("profiler_summary"):
        return
    logger.warning(
        "profiler_summary empty after workflow run — profiler step likely failed; "
        "attempting standalone recovery."
    )
    reports.profiler = _run_profiler_standalone(
        profiler=members.profiler,
        session_state=session_state,
        database_id=database_id,
    )
    if not reports.profiler.tables:
        logger.error(
            "Profiler standalone recovery also produced no output — "
            "statistical and structural reports will be empty for this run."
        )
        return

    known_tables = len(session_state.get("schema") or {})
    profiled_count = len(reports.profiler.tables)
    coverage_ratio = profiled_count / known_tables if known_tables else 1.0
    skip_stat = known_tables > 0 and coverage_ratio < 0.5
    if skip_stat:
        logger.warning(
            "Profiler recovery coverage too low (%d/%d tables, %.0f%%) — "
            "statistical re-run skipped to prevent hallucinations; structural still runs.",
            profiled_count, known_tables, coverage_ratio * 100,
        )

    reports.statistical, reports.structural = _rerun_statistical_structural(
        members=members,
        session_state=session_state,
        database_id=database_id,
        skip_statistical=skip_stat,
    )

    all_reports = {
        "schema":      reports.schema,
        "statistical": reports.statistical,
        "structural":  reports.structural,
        "relational":  reports.relational,
    }
    step_in = _SyntheticStepInput(all_reports, session_state)
    recovered_fdg = reduce_signals(step_in).content
    reports.reduced = _as_reduced_findings(recovered_fdg)

    if not reports.reduced.findings:
        return
    recovered_fix = _rerun_fix_planner(
        fix_planner=members.fix_planner,
        reduced_findings=recovered_fdg,
        session_state=session_state,
        database_id=database_id,
    )
    if recovered_fix is not None:
        reports.fix_plan = recovered_fix


def _build_legacy_reports(
    reports: _InitialReports,
    *,
    pipeline_run_id: str,
    database_id: str,
    run_timestamp: str,
    total_tables: int,
) -> _LegacyReports:
    """Convert each typed report into the legacy dict shape. No side effects."""
    schema_dict = _legacy_validation_report(
        report=reports.schema,
        agent_name="Schema Integrity Validator",
        pipeline_run_id=pipeline_run_id,
        database_id=database_id,
        run_timestamp=run_timestamp,
        total_entities=total_tables,
    )
    statistical_dict = _legacy_validation_report(
        report=reports.statistical,
        agent_name="Statistical Validator",
        pipeline_run_id=pipeline_run_id,
        database_id=database_id,
        run_timestamp=run_timestamp,
        total_entities=total_tables,
    )
    structural_dict = _legacy_validation_report(
        report=reports.structural,
        agent_name="Structural Validator",
        pipeline_run_id=pipeline_run_id,
        database_id=database_id,
        run_timestamp=run_timestamp,
        total_entities=total_tables,
    )
    relational_dict = _legacy_validation_report(
        report=reports.relational,
        agent_name="Relational Validator",
        pipeline_run_id=pipeline_run_id,
        database_id=database_id,
        run_timestamp=run_timestamp,
        total_entities=total_tables,
    )
    fix_dict, legacy_fixes = _legacy_fix_report(
        fix_plan=reports.fix_plan,
        reduced_findings=reports.reduced,
        pipeline_run_id=pipeline_run_id,
        database_id=database_id,
        run_timestamp=run_timestamp,
    )
    return _LegacyReports(
        schema=schema_dict,
        statistical=statistical_dict,
        structural=structural_dict,
        relational=relational_dict,
        fix=fix_dict,
        legacy_fixes=legacy_fixes,
    )


def _build_summary_artifacts(
    legacy: _LegacyReports,
    reports: _InitialReports,
    *,
    session_state: dict[str, Any],
    workflow_result: Any,
    started_at: datetime,
    pipeline_t0: float,
) -> _SummaryArtifacts:
    """Build executive summary, scores, and timing.

    Score aggregation uses the 4 validators only (NOT the fix planner) —
    including the fix planner would double-weight schema findings because
    each fix mirrors a schema finding one-to-one.

    Mutates session_state['_phase_timing_ms'] when the inner workflow session
    has its own timing dict — required so per-phase timings recorded by
    pre_hooks/post_hooks during agent runs are reflected in the timing report
    (Agno keeps a separate session_data dict from the outer session_state).
    """
    import time as _time

    validator_dicts = [legacy.schema, legacy.statistical, legacy.structural, legacy.relational]
    confidence_score, data_quality_score = aggregate_pipeline_scores(validator_dicts)
    confidence_info = confidence_breakdown(validator_dicts)

    total_tables = len(session_state.get("schema") or {})
    exec_summary = _build_executive_summary(
        schema_dict=legacy.schema,
        statistical_dict=legacy.statistical,
        structural_dict=legacy.structural,
        relational_dict=legacy.relational,
        fix_dict=legacy.fix,
        data_quality_score=data_quality_score,
        total_tables=total_tables,
        profiler_table_count=len(reports.profiler.tables),
    )

    # Merge inner workflow timing into the outer session_state.
    inner_sd = getattr(getattr(workflow_result, "session", None), "session_data", None)
    if isinstance(inner_sd, dict):
        inner_timing = inner_sd.get("_phase_timing_ms")
        if isinstance(inner_timing, dict):
            outer_t = session_state.setdefault("_phase_timing_ms", {})
            outer_t.update(inner_timing)

    ended_at = datetime.now(timezone.utc)
    total_ms = round((_time.perf_counter() - pipeline_t0) * 1000)
    phases = {k: v for k, v in (session_state.get("_phase_timing_ms") or {}).items() if k != "team"}
    bottleneck = max(phases.items(), key=lambda kv: kv[1])[0] if phases else None
    timing_info = {
        "started_at": started_at.isoformat().replace("+00:00", "Z"),
        "ended_at": ended_at.isoformat().replace("+00:00", "Z"),
        "total_duration_ms": total_ms,
        "total_duration_human": f"{total_ms / 1000:.1f}s",
        "phases_ms": phases,
        "bottleneck": bottleneck,
    }

    return _SummaryArtifacts(
        confidence_score=confidence_score,
        data_quality_score=data_quality_score,
        confidence_info=confidence_info,
        exec_summary=exec_summary,
        timing_info=timing_info,
    )


def _assemble_final_payload(
    legacy: _LegacyReports,
    summary: _SummaryArtifacts,
    reports: _InitialReports,
    *,
    pipeline_run_id: str,
    database_id: str,
    resolved_model_id: str,
    run_timestamp: str,
) -> dict[str, Any]:
    """Build the final response dict and persist the bundle to disk.

    Output schema is preserved exactly from the legacy implementation.
    Every key — including ones that look duplicated (e.g. statistical_dict
    spread + json_report holding the same dict) — is part of the contract
    consumed by the frontend.

    save_final_pipeline_report failures fall back to a stub artifact dict
    so the response remains shape-compatible with the success path.
    """
    agent_reports = {
        "schema_integrity_validator": {
            **legacy.schema,
            "report": report_to_markdown(legacy.schema, "Schema Integrity Validator"),
        },
        "statistical_validator": {
            **legacy.statistical,
            "report": report_to_markdown(legacy.statistical, "Statistical Validator"),
        },
        "structural_validator": {
            **legacy.structural,
            "report": report_to_markdown(legacy.structural, "Structural Validator"),
        },
        "relational_validator": {
            **legacy.relational,
            "report": report_to_markdown(legacy.relational, "Relational Validator"),
        },
        "data_fix_planner": {
            **legacy.fix,
            "report": report_to_markdown(legacy.fix, "Data Fix Planner"),
        },
    }

    saved_reports = {
        "schema_integrity_validator": legacy.schema,
        "statistical_validator": legacy.statistical,
        "structural_validator": legacy.structural,
        "relational_validator": legacy.relational,
        "data_fix_planner": legacy.fix,
    }

    download_report_bundle = {
        "pipeline_run_id": pipeline_run_id,
        "database_id": database_id,
        "llm_model": resolved_model_id,
        "run_timestamp": run_timestamp,
        "executive_summary": summary.exec_summary,
        "summary": {
            "confidence_score": summary.confidence_score,
            "data_quality_score": summary.data_quality_score,
            "final_fix_count": len(legacy.legacy_fixes),
            "confidence_explanation": summary.confidence_info,
            "timing": summary.timing_info,
        },
        "reports": saved_reports,
    }

    try:
        report_artifact = save_final_pipeline_report(download_report_bundle)
    except Exception as exc:
        logger.error("Failed to save final report: %s", exc)
        report_artifact = {
            "report_file_path": "",
            "report_file_name": "",
            "report_timestamp": run_timestamp,
            "report_database_id": database_id,
        }

    return {
        "pipeline_run_id": pipeline_run_id,
        "database_id": database_id,
        "llm_model": resolved_model_id,
        "schema_analysis": {
            "report": report_to_markdown(legacy.schema, "Schema Integrity Validator"),
            "json_report": legacy.schema,
        },
        "statistical_analysis": {
            **legacy.statistical,
            "report": report_to_markdown(legacy.statistical, "Statistical Validator"),
            "json_report": legacy.statistical,
        },
        "structural_analysis": {
            **legacy.structural,
            "report": report_to_markdown(legacy.structural, "Structural Validator"),
            "json_report": legacy.structural,
        },
        "relational_analysis": {
            **legacy.relational,
            "report": report_to_markdown(legacy.relational, "Relational Validator"),
            "json_report": legacy.relational,
        },
        "profiler_tables_count": len(reports.profiler.tables),
        "fixes": {
            "fixes": legacy.legacy_fixes,
        },
        "agent_reports": agent_reports,
        "final_report": legacy.fix,
        "download_report_json": download_report_bundle,
        "executive_summary": summary.exec_summary,
        "timing": summary.timing_info,
        "confidence_explanation": summary.confidence_info,
        "confidence_score": summary.confidence_score,
        "data_quality_score": summary.data_quality_score,
        **report_artifact,
    }


def orchestrator_workflow(
    connection_string: str,
    domain_rules: Any,
    job_id: str | None = None,
    llm_model: str | None = None,
    historical_signals: str | None = None,
    learned_patterns: list[dict] | None = None,
) -> dict[str, Any]:
    import time as _time
    pipeline_t0 = _time.perf_counter()
    started_at = datetime.now(timezone.utc)

    resolved_model_id = resolve_model_id(llm_model)
    pipeline_run_id = str(uuid.uuid4())
    database_id = derive_database_id(connection_string)
    run_timestamp = started_at.isoformat().replace("+00:00", "Z")

    from .learned_rules import format_for_prompt, load_learned_rules, prune_learned_rules, save_learned_rules
    _active_learned = load_learned_rules(database_id)

    merged_domain_rules = _merge_domain_rules(domain_rules)
    dependencies = _workflow_dependencies(merged_domain_rules)
    session_state = _initial_session_state(
        connection_string=connection_string,
        database_id=database_id,
        job_id=job_id,
        learned_patterns=learned_patterns,
    )
    session_state["learned_rules"] = format_for_prompt(_active_learned)
    # Stored so log_run_metrics hook can pass it to Langfuse generation spans.
    session_state["model_id"] = resolved_model_id

    workflow, members = build_validation_workflow(
        resolved_model_id=resolved_model_id,
        session_state=session_state,
        dependencies=dependencies,
        job_id=job_id,
    )

    from .langfuse_tracer import finish_pipeline_trace, start_pipeline_trace
    start_pipeline_trace(
        pipeline_run_id=pipeline_run_id,
        job_id=job_id,
        database_id=database_id,
        model_id=resolved_model_id,
    )

    try:
        workflow_result = workflow.run(
            input=_build_workflow_prompt(database_id, historical_signals),
            session_state=session_state,
            dependencies=dependencies,
        )

        step_outputs = _collect_step_outputs(getattr(workflow_result, "step_results", []))
        _update_phase_timing_from_steps(step_outputs, session_state)
        reports = _parse_initial_reports(step_outputs)

        # Schema auto-findings — mutates reports.schema.findings in place.
        _merge_schema_auto_findings_into(reports.schema, session_state)

        # Relational transient retry — replaces reports.relational on success.
        raw_relational = getattr(step_outputs.get("relational"), "content", "")
        reports.relational = _maybe_retry_relational(
            reports.relational,
            raw_relational,
            members=members,
            session_state=session_state,
            database_id=database_id,
        )

        # Profiler auto-findings — mutates reports.statistical.findings in place.
        # Runs BEFORE recovery, matching legacy behavior. When recovery replaces
        # reports.statistical, the merged auto-findings are discarded — preserved
        # as-is to avoid behavior drift.
        _merge_profiler_auto_findings_into(reports.statistical, session_state)

        # Profiler recovery — replaces reports.{profiler, statistical, structural,
        # reduced, fix_plan} when the workflow profiler step failed silently.
        _run_recovery_path_if_needed(
            reports,
            members=members,
            session_state=session_state,
            database_id=database_id,
        )

        total_tables = len(session_state.get("schema") or {})
        legacy = _build_legacy_reports(
            reports,
            pipeline_run_id=pipeline_run_id,
            database_id=database_id,
            run_timestamp=run_timestamp,
            total_tables=total_tables,
        )
        summary = _build_summary_artifacts(
            legacy,
            reports,
            session_state=session_state,
            workflow_result=workflow_result,
            started_at=started_at,
            pipeline_t0=pipeline_t0,
        )

        # Persist structural findings as regression guards for future runs.
        # Convert Finding objects → dicts for the learned_rules writer.
        _structural_dicts = [
            {
                "rule": f.rule,
                "table": f.entity.split(".")[0] if f.entity else "",
                "column": f.entity.split(".")[1] if f.entity and "." in f.entity else None,
                "evidence": f.evidence,
                "remediation_hint": f.suggested_action,
            }
            for f in reports.structural.findings
        ]
        save_learned_rules(database_id, pipeline_run_id, _structural_dicts)
        prune_learned_rules(database_id)

        finish_pipeline_trace(
            data_quality_score=summary.data_quality_score,
            confidence_score=summary.confidence_score,
            total_findings=len(reports.reduced.findings),
            total_fixes=len(legacy.legacy_fixes),
            total_duration_ms=summary.timing_info.get("total_duration_ms"),
        )

        return _assemble_final_payload(
            legacy,
            summary,
            reports,
            pipeline_run_id=pipeline_run_id,
            database_id=database_id,
            resolved_model_id=resolved_model_id,
            run_timestamp=run_timestamp,
        )
    except Exception:
        finish_pipeline_trace(error="pipeline failed — see Django logs")
        raise
