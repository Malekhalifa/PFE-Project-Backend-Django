"""
PostgreSQL validation orchestrator powered by Agno + Gemini.

Pipeline order:
  GuidelineGenerator -> SchemaIntegrityValidator
  GuidelineGenerator -> ColumnValidatorAgent
  GuidelineGenerator -> RowLevelValidator
  GuidelineGenerator -> CrossRowValidator
  GuidelineGenerator -> DataFixPlanner

Each agent emits a universal JSON report and receives prior_reports chain context.
The final DataFixPlanner report is persisted to reports/ as pretty-printed JSON.
"""

import json
import logging
import os
import signal
import threading
import time
import uuid

from agno.agent import Agent
from agno.models.google import Gemini

from .tools import (
    execute_pg_query,
    postgres_tool,
    allocate_query_budget,
    enforce_quality_gate,
)
from .guideline_generator import DatabaseGuidelineGeneratorAgent
from .agent_config import (
    SCHEMA_DESCRIPTION,
    SCHEMA_INSTRUCTIONS,
    COLUMN_VALIDATOR_DESCRIPTION,
    COLUMN_VALIDATOR_INSTRUCTIONS,
    CROSS_ROW_DESCRIPTION,
    CROSS_ROW_INSTRUCTIONS,
    ROW_LEVEL_DESCRIPTION,
    ROW_LEVEL_INSTRUCTIONS,
    FIX_PLANNER_DESCRIPTION,
    FIX_PLANNER_INSTRUCTIONS,
    GUIDELINES_INJECTION_TEMPLATE,
)
from .reporting import (
    parse_json_payload,
    build_schema_report,
    build_column_report,
    build_row_report,
    build_cross_row_report,
    build_fix_planner_report,
    report_to_markdown,
    report_to_context_block,
    aggregate_pipeline_scores,
    clone_json,
    derive_database_id,
)

logger = logging.getLogger(__name__)

_GEMINI_MODEL_ID = os.environ.get("GEMINI_MODEL", "gemini-2.0-flash")
_GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
_VALIDATOR_TOOL_CALL_LIMIT = int(os.environ.get("VALIDATOR_TOOL_CALL_LIMIT", "40"))
_CROSS_ROW_TOOL_CALL_LIMIT = int(os.environ.get("CROSS_ROW_TOOL_CALL_LIMIT", "60"))
_FIX_PLANNER_TOOL_CALL_LIMIT = int(os.environ.get("FIX_PLANNER_TOOL_CALL_LIMIT", "30"))
_PIPELINE_STEP_MAX_RETRIES = max(0, int(os.environ.get("PIPELINE_STEP_MAX_RETRIES", "2")))
_PIPELINE_STEP_RETRY_BASE_DELAY_SECONDS = max(
    0.0,
    float(os.environ.get("PIPELINE_STEP_RETRY_BASE_DELAY_SECONDS", "1.0")),
)
_PIPELINE_STEP_RETRY_BACKOFF_MULTIPLIER = max(
    1.0,
    float(os.environ.get("PIPELINE_STEP_RETRY_BACKOFF_MULTIPLIER", "2.0")),
)
_PIPELINE_STEP_RETRY_MAX_DELAY_SECONDS = max(
    _PIPELINE_STEP_RETRY_BASE_DELAY_SECONDS,
    float(os.environ.get("PIPELINE_STEP_RETRY_MAX_DELAY_SECONDS", "8.0")),
)
_PIPELINE_GATE_MIN_CONFIDENCE = float(os.environ.get("PIPELINE_GATE_MIN_CONFIDENCE", "0.55"))
_PIPELINE_GATE_MIN_EVIDENCE_ROWS = int(os.environ.get("PIPELINE_GATE_MIN_EVIDENCE_ROWS", "0"))
_PIPELINE_GATE_REQUIRE_EVIDENCE_QUERY = (
    os.environ.get("PIPELINE_GATE_REQUIRE_EVIDENCE_QUERY", "false").strip().lower() in {"1", "true", "yes", "on"}
)
_PRIOR_REPORTS_SECTION = "\n\n--- Prior Reports Chain (JSON) ---\n"
_FIX_PLANNER_TIMEOUT_SECONDS = int(os.environ.get("FIX_PLANNER_TIMEOUT_SECONDS", "120"))


_JSON_REPORT_CONTRACT = (
    "Return ONLY valid JSON with top-level fields: pipeline_run_id, database_id, run_timestamp, "
    "agent, summary, metrics, findings, prior_reports. findings must be an array. "
    "Use issue severities exactly: critical|high|medium|low. "
    "Each finding should include clear problem description, impact_estimate, remediation_hint, and evidence when available. "
    "If execution is incomplete, include status='partial' and still return collected metrics/findings."
)


def _apply_quality_gate(report: dict, stage_name: str) -> dict:
    """Attach quality-gate decision and forward only accepted findings downstream."""
    findings = report.get("findings") if isinstance(report, dict) else []
    gate_result = enforce_quality_gate(
        findings=findings if isinstance(findings, list) else [],
        min_confidence=_PIPELINE_GATE_MIN_CONFIDENCE,
        min_evidence_rows=_PIPELINE_GATE_MIN_EVIDENCE_ROWS,
        require_evidence_query=_PIPELINE_GATE_REQUIRE_EVIDENCE_QUERY,
    )

    gated = clone_json(report)
    gated["quality_gate"] = {
        "stage": stage_name,
        "min_confidence": _PIPELINE_GATE_MIN_CONFIDENCE,
        "min_evidence_rows": _PIPELINE_GATE_MIN_EVIDENCE_ROWS,
        "require_evidence_query": _PIPELINE_GATE_REQUIRE_EVIDENCE_QUERY,
        "accepted_count": len(gate_result.get("accepted_findings", [])),
        "blocked_count": len(gate_result.get("blocked", [])),
        "meta": gate_result.get("meta", {}),
    }
    gated["blocked_findings"] = gate_result.get("blocked", [])
    gated["findings"] = gate_result.get("accepted_findings", [])

    if gated.get("blocked_findings"):
        gated["status"] = "partial"
        gated.setdefault("errors", []).append("quality_gate_blocked_low_evidence_or_low_confidence_findings")

    return gated


def _retry_delay_seconds(retry_index: int) -> float:
    """Compute exponential backoff delay for retry #N (1-indexed)."""
    exponent = max(retry_index - 1, 0)
    delay = _PIPELINE_STEP_RETRY_BASE_DELAY_SECONDS * (
        _PIPELINE_STEP_RETRY_BACKOFF_MULTIPLIER ** exponent
    )
    return min(delay, _PIPELINE_STEP_RETRY_MAX_DELAY_SECONDS)


def _make_model(model_id: str | None = None) -> Gemini:
    resolved_model_id = (model_id or _GEMINI_MODEL_ID or "").strip() or _GEMINI_MODEL_ID
    return Gemini(
        id=resolved_model_id,
        api_key=_GEMINI_API_KEY or None,
        temperature=0,
        top_p=1,
        seed=7,
    )


# Backward-compatible helper still used by tests.
def _parse_column_report(raw_report: str) -> tuple[dict, str]:
    parsed, parse_error = parse_json_payload(raw_report)
    if isinstance(parsed, dict):
        normalized_text = json.dumps(parsed, ensure_ascii=False, indent=2)
        return parsed, normalized_text

    fallback = {
        "agent": "ColumnValidatorAgent",
        "database_id": "unknown",
        "run_timestamp": "unknown",
        "tables": [],
        "summary": {
            "total_columns_analyzed": 0,
            "columns_passed": 0,
            "columns_flagged": 0,
            "critical_issues": 0,
            "high_issues": 0,
            "medium_issues": 0,
            "low_issues": 0,
        },
        "error": "invalid_column_validator_json",
        "message": parse_error or "Column validator output was not valid JSON",
        "raw": raw_report,
    }
    return fallback, json.dumps(fallback, ensure_ascii=False, indent=2)


def _run_agent(agent: Agent, prompt: str, timeout_seconds: int | None = None) -> tuple[str, str | None]:
    """Run an agent with optional timeout to prevent hanging."""
    result_container = {"response": None, "error": None, "done": False}

    def _agent_thread():
        try:
            response = agent.run(prompt)
            if hasattr(response, "content") and response.content:
                result_container["response"] = str(response.content)
            else:
                result_container["response"] = str(response)
            result_container["done"] = True
        except Exception as exc:
            logger.error("Agent '%s' failed: %s", agent.name, exc)
            result_container["error"] = str(exc)
            result_container["done"] = True

    if timeout_seconds is None:
        # Synchronous execution without timeout
        _agent_thread()
    else:
        # Run in thread with timeout
        thread = threading.Thread(target=_agent_thread, daemon=True)
        thread.start()
        thread.join(timeout=timeout_seconds)
        if thread.is_alive():
            result_container["error"] = f"Agent '{agent.name}' timed out after {timeout_seconds} seconds"
            logger.error(result_container["error"])

    return result_container["response"] or "", result_container["error"]


def _inject_guidelines(guidelines: str) -> str:
    return GUIDELINES_INJECTION_TEMPLATE.format(guidelines=guidelines)


def _serialize_prior_reports(prior_reports: list[dict]) -> str:
    if not prior_reports:
        return "[]"
    return json.dumps(prior_reports, ensure_ascii=False, indent=2)


def _extract_compact_findings(prior_reports: list[dict]) -> str:
    """Extract a compact summary of findings from prior reports for fix planner."""
    compact_findings = []
    for report in prior_reports:
        if not isinstance(report, dict):
            continue
        findings = report.get("findings", [])
        if not isinstance(findings, list):
            continue
        for finding in findings:
            if not isinstance(finding, dict):
                continue
            table = finding.get("table") or finding.get("table_name") or "unknown"
            severity = finding.get("severity", "medium")
            description = finding.get("description") or finding.get("problem") or "Unknown issue"
            if len(description) > 200:
                description = description[:197] + "..."
            compact_findings.append({"table": table, "severity": severity, "description": description})
    
    if not compact_findings:
        return "No prior findings to remediate."
    
    # Sort by severity: critical, high, medium, low
    severity_order = {"critical": 0, "high": 1, "medium": 2, "low": 3}
    compact_findings.sort(key=lambda f: (severity_order.get(f["severity"], 3), f["table"]))
    
    # Format as compact text
    lines = ["\n---\n**FINDINGS SUMMARY:**\n"]
    current_severity = None
    severity_counts = {}
    
    for finding in compact_findings[:30]:  # Limit to top 30 findings
        sev = finding["severity"]
        if sev != current_severity:
            if current_severity is not None:
                count = severity_counts.get(current_severity, 0)
                if count > 30:
                    lines.append(f"... and {count - 30} more {current_severity} items")
            current_severity = sev
            severity_counts[sev] = severity_counts.get(sev, 0) + 1
            lines.append(f"\n**{sev.upper()}:**")
        lines.append(f"  - {finding['table']}: {finding['description']}")
    
    return "\n".join(lines)


def _compact_report_context(report: dict) -> dict:
    """Keep only summary+findings context and drop nested prior report chains."""
    if not isinstance(report, dict):
        return {}

    compact_report = {
        "pipeline_run_id": report.get("pipeline_run_id"),
        "database_id": report.get("database_id"),
        "run_timestamp": report.get("run_timestamp"),
        "agent": report.get("agent"),
        "status": report.get("status"),
        "summary": clone_json(report.get("summary") if isinstance(report.get("summary"), dict) else {}),
        "findings": clone_json(report.get("findings") if isinstance(report.get("findings"), list) else []),
    }

    if isinstance(report.get("quality_gate"), dict):
        compact_report["quality_gate"] = clone_json(report.get("quality_gate"))
    if isinstance(report.get("errors"), list) and report.get("errors"):
        compact_report["errors"] = clone_json(report.get("errors"))

    return compact_report


def _build_download_report_bundle(
    *,
    pipeline_run_id: str,
    database_id: str,
    llm_model: str,
    schema_report: dict,
    column_report: dict,
    row_report: dict,
    cross_report: dict,
    final_report: dict,
    confidence_score: int,
    data_quality_score: int,
) -> dict:
    return {
        "pipeline_run_id": pipeline_run_id,
        "database_id": database_id,
        "llm_model": llm_model,
        "run_timestamp": final_report.get("run_timestamp"),
        "summary": {
            "confidence_score": confidence_score,
            "data_quality_score": data_quality_score,
        },
        "reports": {
            "schema_integrity_validator": clone_json(schema_report),
            "column_validator": clone_json(column_report),
            "row_level_validator": clone_json(row_report),
            "cross_row_validator": clone_json(cross_report),
            "data_fix_planner": clone_json(final_report),
        },
    }


class _PipelineHooks:
    """Encapsulates job-status and audit logging side effects for pipeline steps."""

    def __init__(
        self,
        *,
        job_id: str | None,
        pipeline_run_id: str,
        update_job_status,
        save_job_guideline,
        audit_log,
    ):
        self.job_id = job_id
        self.pipeline_run_id = pipeline_run_id
        self._update_job_status = update_job_status
        self._save_job_guideline = save_job_guideline
        self._audit_log = audit_log
        self._step_start_times: dict[str, float] = {}

    def emit(self, status: str) -> None:
        if not self.job_id:
            return
        try:
            self._update_job_status(self.job_id, status)
        except Exception:
            pass

    def save_guideline(self, agent_key: str, text: str) -> None:
        if not self.job_id:
            return
        try:
            self._save_job_guideline(self.job_id, agent_key, text)
        except Exception:
            pass

    def _step_started(self, step: str) -> None:
        if not self.job_id:
            return
        self._step_start_times[step] = time.perf_counter()
        try:
            self._audit_log(
                self.job_id,
                "PIPELINE_STEP_STARTED",
                {
                    "step": step,
                    "pipeline_run_id": self.pipeline_run_id,
                    "status_code": 102,
                },
            )
        except Exception:
            pass

    def _step_finished(self, step: str, *, success: bool, error: str | None = None) -> None:
        if not self.job_id:
            return

        started_at = self._step_start_times.pop(step, None)
        duration_ms = int((time.perf_counter() - started_at) * 1000) if started_at is not None else None
        payload = {
            "step": step,
            "pipeline_run_id": self.pipeline_run_id,
            "status_code": 200 if success else 500,
        }
        if duration_ms is not None:
            payload["duration_ms"] = duration_ms
        if error:
            payload["error"] = str(error)

        try:
            self._audit_log(
                self.job_id,
                "PIPELINE_STEP_COMPLETED" if success else "PIPELINE_STEP_FAILED",
                payload,
            )
        except Exception:
            pass

    def _step_retrying(
        self,
        step: str,
        *,
        attempt: int,
        max_attempts: int,
        delay_seconds: float,
        error: str,
    ) -> None:
        if not self.job_id:
            return

        payload = {
            "step": step,
            "pipeline_run_id": self.pipeline_run_id,
            "status_code": 102,
            "attempt": attempt,
            "max_attempts": max_attempts,
            "retry_in_ms": int(delay_seconds * 1000),
            "error": error,
        }
        try:
            self._audit_log(self.job_id, "PIPELINE_STEP_RETRYING", payload)
        except Exception:
            pass

    def run_step(self, *, step: str, running_status: str | None, action):
        self._step_started(step)
        if running_status:
            self.emit(running_status)

        max_attempts = _PIPELINE_STEP_MAX_RETRIES + 1
        attempt = 0

        while attempt < max_attempts:
            attempt += 1
            try:
                result = action()
                self._step_finished(step, success=True)
                return result
            except Exception as exc:
                if attempt >= max_attempts:
                    self._step_finished(step, success=False, error=str(exc))
                    raise

                delay_seconds = _retry_delay_seconds(attempt)
                logger.warning(
                    "Step '%s' failed on attempt %d/%d: %s. Retrying in %.2f seconds.",
                    step,
                    attempt,
                    max_attempts,
                    exc,
                    delay_seconds,
                )
                self._step_retrying(
                    step,
                    attempt=attempt,
                    max_attempts=max_attempts,
                    delay_seconds=delay_seconds,
                    error=str(exc),
                )
                if running_status:
                    self.emit(running_status)
                time.sleep(delay_seconds)


def _build_rules_section(domain_rules: str) -> str:
    return (
        f"\n\n---\n**Domain/Business Rules to Enforce:**\n{domain_rules}"
        if domain_rules
        else ""
    )


def _generate_guidelines(
    *,
    hooks: _PipelineHooks,
    generator: DatabaseGuidelineGeneratorAgent,
    agent_key: str,
    prior_findings: str,
    step_name: str,
    running_status: str,
) -> str:
    guidelines = hooks.run_step(
        step=step_name,
        running_status=running_status,
        action=lambda: generator.generate(target_agent=agent_key, prior_findings=prior_findings),
    )
    hooks.save_guideline(agent_key, guidelines)
    return guidelines


def _run_validator_stage(
    *,
    hooks: _PipelineHooks,
    step_name: str,
    running_status: str,
    agent_name: str,
    description: str,
    instructions: list[str],
    tool_call_limit: int,
    model_id: str,
    prompt: str,
    report_builder,
    pipeline_run_id: str,
    database_id: str,
    prior_reports: list[dict],
) -> dict:
    def _action() -> dict:
        agent = Agent(
            name=agent_name,
            model=_make_model(model_id),
            description=description,
            instructions=instructions,
            tools=[execute_pg_query],
            tool_call_limit=tool_call_limit,
            markdown=False,
        )
        logger.info("Starting validator agent '%s' with tool_call_limit=%d", agent_name, tool_call_limit)
        raw, runtime_error = _run_agent(agent, prompt)
        if runtime_error:
            raise RuntimeError(runtime_error)
        logger.info("Validator agent '%s' completed with %d chars output", agent_name, len(raw or ""))
        report = report_builder(
            raw,
            pipeline_run_id=pipeline_run_id,
            database_id=database_id,
            prior_reports=prior_reports,
            runtime_error=runtime_error,
        )
        return _apply_quality_gate(report, step_name)

    return hooks.run_step(step=step_name, running_status=running_status, action=_action)


def _run_fix_planner_stage(
    *,
    hooks: _PipelineHooks,
    step_name: str,
    running_status: str,
    description: str,
    instructions: list[str],
    tool_call_limit: int,
    model_id: str,
    prompt: str,
    pipeline_run_id: str,
    database_id: str,
    prior_reports: list[dict],
) -> dict:
    def _action() -> dict:
        planner = Agent(
            name="Data Fix Planner",
            model=_make_model(model_id),
            description=description,
            instructions=instructions,
            tools=[],
            tool_call_limit=tool_call_limit,
            markdown=False,
        )
        logger.info("Starting Data Fix Planner with timeout=%d seconds, prompt_size=%d chars", 
                    _FIX_PLANNER_TIMEOUT_SECONDS, len(prompt or ""))
        raw, runtime_error = _run_agent(planner, prompt, timeout_seconds=_FIX_PLANNER_TIMEOUT_SECONDS)
        if runtime_error:
            raise RuntimeError(runtime_error)
        logger.info("Data Fix Planner completed with %d chars output", len(raw or ""))
        return build_fix_planner_report(
            raw,
            pipeline_run_id=pipeline_run_id,
            database_id=database_id,
            prior_reports=prior_reports,
            runtime_error=runtime_error,
        )

    return hooks.run_step(step=step_name, running_status=running_status, action=_action)


def _reallocate_limits_from_schema(schema_report: dict, stage_limits: dict) -> dict:
    schema_metrics = schema_report.get("metrics", {}) if isinstance(schema_report, dict) else {}
    total_tables = int((schema_metrics or {}).get("total_tables", 0) or 0)
    total_columns = int((schema_metrics or {}).get("total_columns", 0) or 0)

    approx_rows = 0
    if total_tables > 0 or total_columns > 0:
        approx_rows = max(total_tables * max(total_columns, 1), total_columns)

    payload = allocate_query_budget(
        table_stats=[{"table": "_schema_scope", "estimated_rows": approx_rows}] if approx_rows > 0 else [],
        default_limits=stage_limits,
    )
    return payload.get("limits", stage_limits)


def _build_final_response(
    *,
    pipeline_run_id: str,
    database_id: str,
    llm_model: str,
    schema_report: dict,
    column_report: dict,
    row_report: dict,
    cross_report: dict,
    final_report: dict,
    confidence_score: int,
    data_quality_score: int,
    report_artifact: dict,
) -> dict:
    schema_markdown = report_to_markdown(schema_report, "Schema Integrity Validator Report")
    column_markdown = report_to_markdown(column_report, "Column Validator Report")
    row_markdown = report_to_markdown(row_report, "Row Level Validator Report")
    cross_markdown = report_to_markdown(cross_report, "Cross Row Validator Report")

    return {
        "pipeline_run_id": pipeline_run_id,
        "database_id": database_id,
        "llm_model": llm_model,
        "schema_analysis": {
            "report": schema_markdown,
            "json_report": schema_report,
        },
        "column_validator_analysis": {
            **column_report,
            "report": column_markdown,
        },
        "row_level_analysis": {
            "report": row_markdown,
            "json_report": row_report,
        },
        "cross_row_analysis": {
            "report": cross_markdown,
            "json_report": cross_report,
        },
        "fixes": {
            "fixes": final_report.get("findings", []),
        },
        "agent_reports": {
            "schema_integrity_validator": schema_report,
            "column_validator": column_report,
            "row_level_validator": row_report,
            "cross_row_validator": cross_report,
            "data_fix_planner": final_report,
        },
        "final_report": final_report,
        "download_report_json": _build_download_report_bundle(
            pipeline_run_id=pipeline_run_id,
            database_id=database_id,
            llm_model=llm_model,
            schema_report=schema_report,
            column_report=column_report,
            row_report=row_report,
            cross_report=cross_report,
            final_report=final_report,
            confidence_score=confidence_score,
            data_quality_score=data_quality_score,
        ),
        "confidence_score": confidence_score,
        "data_quality_score": data_quality_score,
        **report_artifact,
    }


def orchestrator_workflow(
    connection_string: str,
    domain_rules: str,
    job_id: str | None = None,
    llm_model: str | None = None,
) -> dict:
    """
    Main entry point for Celery tasks.

    Returns a backwards-compatible payload for existing UI consumers plus:
      - agent_reports: universal reports for all pipeline stages
      - final_report: DataFixPlanner universal report (saved to disk)
      - report artifact metadata for download wiring
    """
    from api.db import update_job_status, save_job_guideline  # avoid circular import
    from api.models import audit_log
    from services.report_service import save_final_pipeline_report

    postgres_tool.connect(connection_string)

    resolved_model_id = (llm_model or _GEMINI_MODEL_ID or "").strip() or _GEMINI_MODEL_ID
    generator = DatabaseGuidelineGeneratorAgent(
        connection_string=connection_string,
        model_id=resolved_model_id,
        api_key=_GEMINI_API_KEY,
    )

    rules_section = _build_rules_section(domain_rules)

    pipeline_run_id = str(uuid.uuid4())
    database_id = derive_database_id(connection_string)
    hooks = _PipelineHooks(
        job_id=job_id,
        pipeline_run_id=pipeline_run_id,
        update_job_status=update_job_status,
        save_job_guideline=save_job_guideline,
        audit_log=audit_log,
    )

    prior_findings = ""
    prior_reports: list[dict] = []

    stage_limits_payload = allocate_query_budget(
        table_stats=[],
        default_limits={
            "schema": _VALIDATOR_TOOL_CALL_LIMIT,
            "column": _VALIDATOR_TOOL_CALL_LIMIT,
            "row": _VALIDATOR_TOOL_CALL_LIMIT,
            "cross_row": _CROSS_ROW_TOOL_CALL_LIMIT,
            "fix_planner": _FIX_PLANNER_TOOL_CALL_LIMIT,
        },
    )
    stage_limits = stage_limits_payload.get("limits", {})

    logger.info(
        "Pipeline started with pipeline_run_id=%s database_id=%s model=%s",
        pipeline_run_id,
        database_id,
        resolved_model_id,
    )

    schema_guidelines = _generate_guidelines(
        hooks=hooks,
        generator=generator,
        agent_key="schema_integrity_validator",
        prior_findings=prior_findings,
        step_name="guideline_gen_schema",
        running_status="running_guideline_gen_schema",
    )

    schema_prompt = (
        _inject_guidelines(schema_guidelines)
        + "Perform schema integrity validation using live SELECT-only introspection. "
        + _JSON_REPORT_CONTRACT
        + "\n\nFor every finding, provide concrete structural impact and actionable remediation guidance."
        + "\n\nPopulate schema metrics exactly: total_tables, total_columns, tables_with_no_primary_key, "
        + "missing_foreign_keys, constraint_violations, type_mismatches, nullable_violations."
        + _PRIOR_REPORTS_SECTION
        + _serialize_prior_reports(prior_reports)
        + rules_section
    )
    schema_report = _run_validator_stage(
        hooks=hooks,
        step_name="schema_validation",
        running_status="running_schema",
        agent_name="Schema Integrity Validator",
        description=SCHEMA_DESCRIPTION,
        instructions=SCHEMA_INSTRUCTIONS,
        tool_call_limit=int(stage_limits.get("schema", _VALIDATOR_TOOL_CALL_LIMIT)),
        model_id=resolved_model_id,
        prompt=schema_prompt,
        report_builder=build_schema_report,
        pipeline_run_id=pipeline_run_id,
        database_id=database_id,
        prior_reports=prior_reports,
    )

    stage_limits = _reallocate_limits_from_schema(schema_report, stage_limits)
    prior_reports.append(_compact_report_context(schema_report))
    prior_findings = report_to_context_block(schema_report, "Schema Integrity Findings")

    column_guidelines = _generate_guidelines(
        hooks=hooks,
        generator=generator,
        agent_key="column_validator",
        prior_findings=prior_findings,
        step_name="guideline_gen_column",
        running_status="running_guideline_gen_column",
    )

    column_prompt = (
        _inject_guidelines(column_guidelines)
        + "Perform column-level validation with structured findings only. "
        + _JSON_REPORT_CONTRACT
        + "\n\nFor every finding, include business impact, remediation_hint, and evidence sampled from executed queries."
        + "\n\nPopulate column metrics exactly: total_columns_analyzed, duplicate_columns_found, "
        + "constant_columns_found, high_null_ratio_columns, low_cardinality_anomalies, "
        + "high_cardinality_anomalies, semantic_mismatches, domain_violations, "
        + "distribution_anomalies, avg_column_quality_score, avg_column_confidence."
        + "\n\n--- Schema Report (JSON) ---\n"
        + json.dumps(schema_report, ensure_ascii=False, indent=2)
        + _PRIOR_REPORTS_SECTION
        + _serialize_prior_reports(prior_reports)
        + rules_section
    )
    column_report = _run_validator_stage(
        hooks=hooks,
        step_name="column_validation",
        running_status="running_column",
        agent_name="Column Validator",
        description=COLUMN_VALIDATOR_DESCRIPTION,
        instructions=COLUMN_VALIDATOR_INSTRUCTIONS,
        tool_call_limit=int(stage_limits.get("column", _VALIDATOR_TOOL_CALL_LIMIT)),
        model_id=resolved_model_id,
        prompt=column_prompt,
        report_builder=build_column_report,
        pipeline_run_id=pipeline_run_id,
        database_id=database_id,
        prior_reports=prior_reports,
    )
    prior_reports.append(_compact_report_context(column_report))
    prior_findings += "\n\n" + report_to_context_block(column_report, "Column Findings")

    row_guidelines = _generate_guidelines(
        hooks=hooks,
        generator=generator,
        agent_key="row_level_validator",
        prior_findings=prior_findings,
        step_name="guideline_gen_row_level",
        running_status="running_guideline_gen_row_level",
    )

    row_prompt = (
        _inject_guidelines(row_guidelines)
        + "Perform row-level validation and return structured JSON findings. "
        + _JSON_REPORT_CONTRACT
        + "\n\nFor every finding, include practical remediation_hint and explain likely downstream impact."
        + "\n\nPopulate row metrics exactly: total_rows_analyzed, rows_with_issues, rows_with_issues_ratio, "
        + "format_violations, encoding_issues, hidden_null_count, outlier_rows, "
        + "avg_row_quality_score, avg_row_confidence."
        + _PRIOR_REPORTS_SECTION
        + _serialize_prior_reports(prior_reports)
        + rules_section
    )
    row_report = _run_validator_stage(
        hooks=hooks,
        step_name="row_level_validation",
        running_status="running_row_level",
        agent_name="Row Level Validator",
        description=ROW_LEVEL_DESCRIPTION,
        instructions=ROW_LEVEL_INSTRUCTIONS,
        tool_call_limit=int(stage_limits.get("row", _VALIDATOR_TOOL_CALL_LIMIT)),
        model_id=resolved_model_id,
        prompt=row_prompt,
        report_builder=build_row_report,
        pipeline_run_id=pipeline_run_id,
        database_id=database_id,
        prior_reports=prior_reports,
    )
    prior_reports.append(_compact_report_context(row_report))
    prior_findings += "\n\n" + report_to_context_block(row_report, "Row Level Findings")

    cross_guidelines = _generate_guidelines(
        hooks=hooks,
        generator=generator,
        agent_key="cross_row_validator",
        prior_findings=prior_findings,
        step_name="guideline_gen_cross_row",
        running_status="running_guideline_gen_cross_row",
    )

    cross_prompt = (
        _inject_guidelines(cross_guidelines)
        + "Perform cross-row validation and return structured JSON findings. "
        + _JSON_REPORT_CONTRACT
        + "\n\nFor every finding, include impact_estimate and remediation_hint grounded in observed relationships."
        + "\n\nPopulate cross-row metrics exactly: total_row_pairs_analyzed, temporal_anomalies, sequential_breaks, "
        + "cross_column_violations, row_pattern_anomalies, tables_with_cross_row_issues, avg_relationship_confidence."
        + _PRIOR_REPORTS_SECTION
        + _serialize_prior_reports(prior_reports)
        + rules_section
    )
    cross_report = _run_validator_stage(
        hooks=hooks,
        step_name="cross_row_validation",
        running_status="running_cross_row",
        agent_name="Cross Row Validator",
        description=CROSS_ROW_DESCRIPTION,
        instructions=CROSS_ROW_INSTRUCTIONS,
        tool_call_limit=int(stage_limits.get("cross_row", _CROSS_ROW_TOOL_CALL_LIMIT)),
        model_id=resolved_model_id,
        prompt=cross_prompt,
        report_builder=build_cross_row_report,
        pipeline_run_id=pipeline_run_id,
        database_id=database_id,
        prior_reports=prior_reports,
    )
    prior_reports.append(_compact_report_context(cross_report))
    prior_findings += "\n\n" + report_to_context_block(cross_report, "Cross Row Findings")

    fix_guidelines = _generate_guidelines(
        hooks=hooks,
        generator=generator,
        agent_key="data_fix_planner",
        prior_findings=prior_findings,
        step_name="guideline_gen_fix_planner",
        running_status="running_guideline_gen_fix_planner",
    )

    fix_prompt = (
        _inject_guidelines(fix_guidelines)
        + "Generate fix planning output as structured JSON findings (one finding per fix card). "
        + _JSON_REPORT_CONTRACT
        + "\n\nInclude clear plain-language problem statements and operational impact for each fix card."
        + "\n\nPopulate fix planner metrics exactly: total_issues_received, fixes_generated, critical_fixes, "
        + "high_fixes, medium_fixes, low_fixes, estimated_rows_affected, fix_confidence."
        + "\n\nIssues identified in prior validation stages:"
        + _extract_compact_findings(prior_reports)
        + "\n\nFor each issue above, create a fix card with appropriate SQL remediation steps."
        + rules_section
    )
    final_report = _run_fix_planner_stage(
        hooks=hooks,
        step_name="fix_planner",
        running_status="running_fix_planner",
        description=FIX_PLANNER_DESCRIPTION,
        instructions=FIX_PLANNER_INSTRUCTIONS,
        tool_call_limit=int(stage_limits.get("fix_planner", _FIX_PLANNER_TOOL_CALL_LIMIT)),
        model_id=resolved_model_id,
        prompt=fix_prompt,
        pipeline_run_id=pipeline_run_id,
        database_id=database_id,
        prior_reports=prior_reports,
    )

    hooks.run_step(
        step="saving_guidelines",
        running_status="saving_guidelines",
        action=generator.save_guidelines,
    )

    confidence_score, data_quality_score = aggregate_pipeline_scores(
        [schema_report, column_report, row_report, cross_report]
    )

    download_report_bundle = _build_download_report_bundle(
        pipeline_run_id=pipeline_run_id,
        database_id=database_id,
        llm_model=resolved_model_id,
        schema_report=schema_report,
        column_report=column_report,
        row_report=row_report,
        cross_report=cross_report,
        final_report=final_report,
        confidence_score=confidence_score,
        data_quality_score=data_quality_score,
    )

    report_artifact = {
        "report_file_path": "",
        "report_file_name": "",
        "report_timestamp": final_report.get("run_timestamp"),
        "report_database_id": final_report.get("database_id"),
    }
    try:
        report_artifact = save_final_pipeline_report(download_report_bundle)
    except Exception as exc:
        logger.error("Failed to save final report file: %s", exc)
        final_report["status"] = "partial"
        final_report.setdefault("errors", []).append(f"report_save_failed: {exc}")

    response = _build_final_response(
        pipeline_run_id=pipeline_run_id,
        database_id=database_id,
        llm_model=resolved_model_id,
        schema_report=schema_report,
        column_report=column_report,
        row_report=row_report,
        cross_report=cross_report,
        final_report=final_report,
        confidence_score=confidence_score,
        data_quality_score=data_quality_score,
        report_artifact=report_artifact,
    )
    response["download_report_json"] = download_report_bundle
    return response
