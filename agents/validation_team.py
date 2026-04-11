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
import uuid

from agno.agent import Agent
from agno.models.google import Gemini

from .tools import execute_pg_query, postgres_tool
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
_PRIOR_REPORTS_SECTION = "\n\n--- Prior Reports Chain (JSON) ---\n"


_JSON_REPORT_CONTRACT = (
    "Return ONLY valid JSON with top-level fields: pipeline_run_id, database_id, run_timestamp, "
    "agent, summary, metrics, findings, prior_reports. findings must be an array. "
    "Use issue severities exactly: critical|high|medium|low. "
    "If execution is incomplete, include status='partial' and still return collected metrics/findings."
)


def _make_model() -> Gemini:
    return Gemini(
        id=_GEMINI_MODEL_ID,
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


def _run_agent(agent: Agent, prompt: str) -> tuple[str, str | None]:
    try:
        response = agent.run(prompt)
        if hasattr(response, "content") and response.content:
            return str(response.content), None
        return str(response), None
    except Exception as exc:
        logger.error("Agent '%s' failed: %s", agent.name, exc)
        return "", str(exc)


def _inject_guidelines(guidelines: str) -> str:
    return GUIDELINES_INJECTION_TEMPLATE.format(guidelines=guidelines)


def _serialize_prior_reports(prior_reports: list[dict]) -> str:
    if not prior_reports:
        return "[]"
    return json.dumps(prior_reports, ensure_ascii=False, indent=2)


def _build_download_report_bundle(
    *,
    pipeline_run_id: str,
    database_id: str,
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


def orchestrator_workflow(
    connection_string: str, domain_rules: str, job_id: str | None = None
) -> dict:
    """
    Main entry point for Celery tasks.

    Returns a backwards-compatible payload for existing UI consumers plus:
      - agent_reports: universal reports for all pipeline stages
      - final_report: DataFixPlanner universal report (saved to disk)
      - report artifact metadata for download wiring
    """
    from api.db import update_job_status, save_job_guideline  # avoid circular import
    from services.report_service import save_final_pipeline_report

    def _emit(status: str):
        if job_id:
            try:
                update_job_status(job_id, status)
            except Exception:
                pass

    def _save_guideline(agent_key: str, text: str):
        if job_id:
            try:
                save_job_guideline(job_id, agent_key, text)
            except Exception:
                pass

    postgres_tool.connect(connection_string)

    generator = DatabaseGuidelineGeneratorAgent(
        connection_string=connection_string,
        model_id=_GEMINI_MODEL_ID,
        api_key=_GEMINI_API_KEY,
    )

    rules_section = (
        f"\n\n---\n**Domain/Business Rules to Enforce:**\n{domain_rules}"
        if domain_rules
        else ""
    )

    pipeline_run_id = str(uuid.uuid4())
    database_id = derive_database_id(connection_string)

    prior_findings = ""
    prior_reports: list[dict] = []

    logger.info("Pipeline started with pipeline_run_id=%s database_id=%s", pipeline_run_id, database_id)

    # ------------------------------------------------------------------
    # AGENT 1 - SchemaIntegrityValidator
    # ------------------------------------------------------------------
    _emit("running_guideline_gen_schema")
    schema_guidelines = generator.generate(
        target_agent="schema_integrity_validator",
        prior_findings=prior_findings,
    )
    _save_guideline("schema_integrity_validator", schema_guidelines)

    _emit("running_schema")
    schema_agent = Agent(
        name="Schema Integrity Validator",
        model=_make_model(),
        description=SCHEMA_DESCRIPTION,
        instructions=SCHEMA_INSTRUCTIONS,
        tools=[execute_pg_query],
        tool_call_limit=_VALIDATOR_TOOL_CALL_LIMIT,
        markdown=False,
    )
    schema_prompt = (
        _inject_guidelines(schema_guidelines)
        + "Perform schema integrity validation using live SELECT-only introspection. "
        + _JSON_REPORT_CONTRACT
        + "\n\nPopulate schema metrics exactly: total_tables, total_columns, tables_with_no_primary_key, "
        + "missing_foreign_keys, constraint_violations, type_mismatches, nullable_violations."
        + _PRIOR_REPORTS_SECTION
        + _serialize_prior_reports(prior_reports)
        + rules_section
    )
    schema_raw, schema_runtime_error = _run_agent(schema_agent, schema_prompt)
    schema_report = build_schema_report(
        schema_raw,
        pipeline_run_id=pipeline_run_id,
        database_id=database_id,
        prior_reports=prior_reports,
        runtime_error=schema_runtime_error,
    )
    prior_reports.append(clone_json(schema_report))
    prior_findings = report_to_context_block(schema_report, "Schema Integrity Findings")

    # ------------------------------------------------------------------
    # AGENT 2 - ColumnValidatorAgent
    # ------------------------------------------------------------------
    _emit("running_guideline_gen_column")
    column_guidelines = generator.generate(
        target_agent="column_validator",
        prior_findings=prior_findings,
    )
    _save_guideline("column_validator", column_guidelines)

    _emit("running_column")
    column_agent = Agent(
        name="Column Validator",
        model=_make_model(),
        description=COLUMN_VALIDATOR_DESCRIPTION,
        instructions=COLUMN_VALIDATOR_INSTRUCTIONS,
        tools=[execute_pg_query],
        tool_call_limit=_VALIDATOR_TOOL_CALL_LIMIT,
        markdown=False,
    )
    column_prompt = (
        _inject_guidelines(column_guidelines)
        + "Perform column-level validation with structured findings only. "
        + _JSON_REPORT_CONTRACT
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
    column_raw, column_runtime_error = _run_agent(column_agent, column_prompt)
    column_report = build_column_report(
        column_raw,
        pipeline_run_id=pipeline_run_id,
        database_id=database_id,
        prior_reports=prior_reports,
        runtime_error=column_runtime_error,
    )
    prior_reports.append(clone_json(column_report))
    prior_findings += "\n\n" + report_to_context_block(column_report, "Column Findings")

    # ------------------------------------------------------------------
    # AGENT 3 - RowLevelValidator
    # ------------------------------------------------------------------
    _emit("running_guideline_gen_row_level")
    row_guidelines = generator.generate(
        target_agent="row_level_validator",
        prior_findings=prior_findings,
    )
    _save_guideline("row_level_validator", row_guidelines)

    _emit("running_row_level")
    row_agent = Agent(
        name="Row Level Validator",
        model=_make_model(),
        description=ROW_LEVEL_DESCRIPTION,
        instructions=ROW_LEVEL_INSTRUCTIONS,
        tools=[execute_pg_query],
        tool_call_limit=_VALIDATOR_TOOL_CALL_LIMIT,
        markdown=False,
    )
    row_prompt = (
        _inject_guidelines(row_guidelines)
        + "Perform row-level validation and return structured JSON findings. "
        + _JSON_REPORT_CONTRACT
        + "\n\nPopulate row metrics exactly: total_rows_analyzed, rows_with_issues, rows_with_issues_ratio, "
        + "format_violations, encoding_issues, hidden_null_count, outlier_rows, "
        + "avg_row_quality_score, avg_row_confidence."
        + _PRIOR_REPORTS_SECTION
        + _serialize_prior_reports(prior_reports)
        + rules_section
    )
    row_raw, row_runtime_error = _run_agent(row_agent, row_prompt)
    row_report = build_row_report(
        row_raw,
        pipeline_run_id=pipeline_run_id,
        database_id=database_id,
        prior_reports=prior_reports,
        runtime_error=row_runtime_error,
    )
    prior_reports.append(clone_json(row_report))
    prior_findings += "\n\n" + report_to_context_block(row_report, "Row Level Findings")

    # ------------------------------------------------------------------
    # AGENT 4 - CrossRowValidator
    # ------------------------------------------------------------------
    _emit("running_guideline_gen_cross_row")
    cross_guidelines = generator.generate(
        target_agent="cross_row_validator",
        prior_findings=prior_findings,
    )
    _save_guideline("cross_row_validator", cross_guidelines)

    _emit("running_cross_row")
    cross_agent = Agent(
        name="Cross Row Validator",
        model=_make_model(),
        description=CROSS_ROW_DESCRIPTION,
        instructions=CROSS_ROW_INSTRUCTIONS,
        tools=[execute_pg_query],
        tool_call_limit=_CROSS_ROW_TOOL_CALL_LIMIT,
        markdown=False,
    )
    cross_prompt = (
        _inject_guidelines(cross_guidelines)
        + "Perform cross-row validation and return structured JSON findings. "
        + _JSON_REPORT_CONTRACT
        + "\n\nPopulate cross-row metrics exactly: total_row_pairs_analyzed, temporal_anomalies, sequential_breaks, "
        + "cross_column_violations, row_pattern_anomalies, tables_with_cross_row_issues, avg_relationship_confidence."
        + _PRIOR_REPORTS_SECTION
        + _serialize_prior_reports(prior_reports)
        + rules_section
    )
    cross_raw, cross_runtime_error = _run_agent(cross_agent, cross_prompt)
    cross_report = build_cross_row_report(
        cross_raw,
        pipeline_run_id=pipeline_run_id,
        database_id=database_id,
        prior_reports=prior_reports,
        runtime_error=cross_runtime_error,
    )
    prior_reports.append(clone_json(cross_report))
    prior_findings += "\n\n" + report_to_context_block(cross_report, "Cross Row Findings")

    # ------------------------------------------------------------------
    # AGENT 5 - DataFixPlanner
    # ------------------------------------------------------------------
    _emit("running_guideline_gen_fix_planner")
    fix_guidelines = generator.generate(
        target_agent="data_fix_planner",
        prior_findings=prior_findings,
    )
    _save_guideline("data_fix_planner", fix_guidelines)

    _emit("running_fix_planner")
    fix_planner = Agent(
        name="Data Fix Planner",
        model=_make_model(),
        description=FIX_PLANNER_DESCRIPTION,
        instructions=FIX_PLANNER_INSTRUCTIONS,
        tools=[],
        tool_call_limit=_FIX_PLANNER_TOOL_CALL_LIMIT,
        markdown=False,
    )
    fix_prompt = (
        _inject_guidelines(fix_guidelines)
        + "Generate fix planning output as structured JSON findings (one finding per fix card). "
        + _JSON_REPORT_CONTRACT
        + "\n\nPopulate fix planner metrics exactly: total_issues_received, fixes_generated, critical_fixes, "
        + "high_fixes, medium_fixes, low_fixes, estimated_rows_affected, fix_confidence."
        + "\n\nUse prior reports chain as the only source of truth for issue accumulation."
        + _PRIOR_REPORTS_SECTION
        + _serialize_prior_reports(prior_reports)
    )
    fix_raw, fix_runtime_error = _run_agent(fix_planner, fix_prompt)
    final_report = build_fix_planner_report(
        fix_raw,
        pipeline_run_id=pipeline_run_id,
        database_id=database_id,
        prior_reports=prior_reports,
        runtime_error=fix_runtime_error,
    )

    # Persist guidelines version after all phases complete.
    _emit("saving_guidelines")
    generator.save_guidelines()

    confidence_score, data_quality_score = aggregate_pipeline_scores(
        [schema_report, column_report, row_report, cross_report]
    )

    download_report_bundle = _build_download_report_bundle(
        pipeline_run_id=pipeline_run_id,
        database_id=database_id,
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

    schema_markdown = report_to_markdown(schema_report, "Schema Integrity Validator Report")
    column_markdown = report_to_markdown(column_report, "Column Validator Report")
    row_markdown = report_to_markdown(row_report, "Row Level Validator Report")
    cross_markdown = report_to_markdown(cross_report, "Cross Row Validator Report")

    return {
        "pipeline_run_id": pipeline_run_id,
        "database_id": database_id,
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
        "download_report_json": download_report_bundle,
        "confidence_score": confidence_score,
        "data_quality_score": data_quality_score,
        **report_artifact,
    }
