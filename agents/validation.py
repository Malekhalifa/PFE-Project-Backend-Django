"""
validation.py - Workflow-backed QualityLab pipeline with legacy response adapters.
"""

from __future__ import annotations

import logging
import re
import uuid
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from agno.db.sqlite import SqliteDb
from agno.workflow.parallel import Parallel
from agno.workflow.step import Step
from agno.workflow.types import StepOutput
from agno.workflow.workflow import Workflow

from services.report_service import save_final_pipeline_report

from .reducer import _try_recover_partial, reduce_signals
from .reporting import aggregate_pipeline_scores, derive_database_id, report_to_markdown
from .rules import common_rules, policy_rules
from .schemas import Finding, FixCard, FixDependencyGraph, FixPlan, ProfilerReport, ReducedFindings, ValidationReport
from .team import build_members, resolve_model_id

logger = logging.getLogger(__name__)

_WORKFLOW_DB_PATH = Path(__file__).resolve().parent.parent / "tmp" / "qualitylab.db"
_FIX_ID_PATTERN = re.compile(r"[^a-z0-9]+")


def _workflow_db(job_id: str | None = None) -> SqliteDb:
    _WORKFLOW_DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    if job_id:
        path = _WORKFLOW_DB_PATH.parent / f"qualitylab_{job_id}.db"
    else:
        path = _WORKFLOW_DB_PATH
    return SqliteDb(db_file=str(path))


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
        "policy_rules": policy_rules,
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
        "last_reduced_findings": [],
        "_tool_calls_seen": {},
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


def build_validation_workflow(
    *,
    resolved_model_id: str,
    session_state: dict[str, Any],
    dependencies: dict[str, Any],
    job_id: str | None = None,
) -> Workflow:
    db = _workflow_db(job_id)
    members = build_members(
        model_id=resolved_model_id,
        ql_mode="pipeline",
        db=db,
        session_state=session_state,
        dependencies=dependencies,
    )
    return Workflow(
        name="QualityLab Validation",
        db=db,
        session_state=session_state,
        dependencies=dependencies,
        steps=[
            Step(name="schema",   agent=members.schema_validator),
            Step(name="profiler", agent=members.profiler),
            Parallel(
                Step(name="statistical", agent=members.statistical_validator),
                Step(name="structural",  agent=members.structural_validator),
                Step(name="relational",  agent=members.relational_validator),
                name="Validation Phase",
            ),
            Step(name="reduce",   executor=reduce_signals),
            Step(name="fix_plan", agent=members.fix_planner),
        ],
    )


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


def _quality_score(findings: list[Finding]) -> float:
    penalty = 0
    weights = {"critical": 25, "high": 15, "medium": 8, "low": 3}
    for finding in findings:
        penalty += weights.get(finding.severity, 0)
    return float(max(0, 100 - penalty))


def _legacy_validation_finding(finding: Finding) -> dict[str, Any]:
    table_name, column_name = _split_entity(finding.entity)
    issue_description = finding.suggested_action or finding.rule.replace("_", " ")
    return {
        "table": table_name,
        "column": column_name,
        "entity": finding.entity,
        "rule": finding.rule,
        "severity": finding.severity,
        "confidence": finding.confidence,
        "evidence": finding.evidence,
        "remediation_hint": finding.suggested_action,
        "issues": [
            {
                "type": finding.rule,
                "severity": finding.severity,
                "description": issue_description,
                "evidence": finding.evidence,
            }
        ],
    }


def _legacy_validation_report(
    *,
    report: ValidationReport,
    agent_name: str,
    pipeline_run_id: str,
    database_id: str,
    run_timestamp: str,
    total_entities: int | None = None,
) -> dict[str, Any]:
    findings = [_legacy_validation_finding(item) for item in report.findings]
    severity = _severity_summary(report.findings)
    flagged_tables = sorted({item["table"] for item in findings if item.get("table")})
    avg_confidence = (
        round(sum(item.confidence for item in report.findings) / len(report.findings), 4)
        if report.findings
        else 1.0
    )
    quality_score = _quality_score(report.findings)
    items_flagged = len(report.findings)
    items_total = total_entities if total_entities is not None else items_flagged
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
        },
        "findings": findings,
        "prior_reports": [],
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
        "severity": matched.severity if matched else "medium",
        "confidence": matched.confidence if matched else 0.75,
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
            "overall_quality_score": float(max(0, 100 - (len(fixes) * 5))),
            "overall_confidence": avg_confidence,
            "flagged_tables": sorted({item["table"] for item in fixes if item.get("table")}),
        },
        "metrics": {
            "total_issues_received": len(reduced_findings.findings),
            "fixes_generated": len(fixes),
        },
        "findings": fixes,
        "cards": [card.model_dump(mode="json") for card in fix_plan.cards],
    }
    return report, fixes


def orchestrator_workflow(
    connection_string: str,
    domain_rules: Any,
    job_id: str | None = None,
    llm_model: str | None = None,
    historical_signals: str | None = None,
    learned_patterns: list[dict] | None = None,
) -> dict[str, Any]:
    resolved_model_id = resolve_model_id(llm_model)
    pipeline_run_id = str(uuid.uuid4())
    database_id = derive_database_id(connection_string)
    run_timestamp = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

    merged_domain_rules = _merge_domain_rules(domain_rules)
    dependencies = _workflow_dependencies(merged_domain_rules)
    session_state = _initial_session_state(
        connection_string=connection_string,
        database_id=database_id,
        job_id=job_id,
        learned_patterns=learned_patterns,
    )

    workflow = build_validation_workflow(
        resolved_model_id=resolved_model_id,
        session_state=session_state,
        dependencies=dependencies,
        job_id=job_id,
    )
    workflow_result = workflow.run(
        input=_build_workflow_prompt(database_id, historical_signals),
        session_state=session_state,
        dependencies=dependencies,
    )

    step_outputs = _collect_step_outputs(getattr(workflow_result, "step_results", []))
    schema_report      = _as_validation_report(getattr(step_outputs.get("schema"),      "content", ""), "schema")
    profiler_report    = _as_profiler_report(  getattr(step_outputs.get("profiler"),     "content", ""))
    statistical_report = _as_validation_report(getattr(step_outputs.get("statistical"), "content", ""), "statistical")
    structural_report  = _as_validation_report(getattr(step_outputs.get("structural"),  "content", ""), "structural")
    relational_report  = _as_validation_report(getattr(step_outputs.get("relational"),  "content", ""), "relational")
    reduced_findings   = _as_reduced_findings( getattr(step_outputs.get("reduce"),      "content", ""))
    fix_plan           = _as_fix_plan(         getattr(step_outputs.get("fix_plan"),    "content", ""))

    # Total tables inspected is available from the schema priming hook via session_state.
    total_tables = len(session_state.get("schema") or {})

    schema_dict = _legacy_validation_report(
        report=schema_report,
        agent_name="Schema Integrity Validator",
        pipeline_run_id=pipeline_run_id,
        database_id=database_id,
        run_timestamp=run_timestamp,
        total_entities=total_tables,
    )
    statistical_dict = _legacy_validation_report(
        report=statistical_report,
        agent_name="Statistical Validator",
        pipeline_run_id=pipeline_run_id,
        database_id=database_id,
        run_timestamp=run_timestamp,
        total_entities=total_tables,
    )
    structural_dict = _legacy_validation_report(
        report=structural_report,
        agent_name="Structural Validator",
        pipeline_run_id=pipeline_run_id,
        database_id=database_id,
        run_timestamp=run_timestamp,
        total_entities=total_tables,
    )
    relational_dict = _legacy_validation_report(
        report=relational_report,
        agent_name="Relational Validator",
        pipeline_run_id=pipeline_run_id,
        database_id=database_id,
        run_timestamp=run_timestamp,
        total_entities=total_tables,
    )
    fix_dict, legacy_fixes = _legacy_fix_report(
        fix_plan=fix_plan,
        reduced_findings=reduced_findings,
        pipeline_run_id=pipeline_run_id,
        database_id=database_id,
        run_timestamp=run_timestamp,
    )

    confidence_score, data_quality_score = aggregate_pipeline_scores(
        [schema_dict, statistical_dict, structural_dict, relational_dict, fix_dict]
    )

    # Build agent_reports with markdown report fields so the frontend's
    # agent panel can render each validator's output without a fallback.
    agent_reports = {
        "schema_integrity_validator": {
            **schema_dict,
            "report": report_to_markdown(schema_dict, "Schema Integrity Validator"),
        },
        "statistical_validator": {
            **statistical_dict,
            "report": report_to_markdown(statistical_dict, "Statistical Validator"),
        },
        "structural_validator": {
            **structural_dict,
            "report": report_to_markdown(structural_dict, "Structural Validator"),
        },
        "relational_validator": {
            **relational_dict,
            "report": report_to_markdown(relational_dict, "Relational Validator"),
        },
        "data_fix_planner": {
            **fix_dict,
            "report": report_to_markdown(fix_dict, "Data Fix Planner"),
        },
    }

    download_report_bundle = {
        "pipeline_run_id": pipeline_run_id,
        "database_id": database_id,
        "llm_model": resolved_model_id,
        "run_timestamp": run_timestamp,
        "summary": {
            "confidence_score": confidence_score,
            "data_quality_score": data_quality_score,
            "final_fix_count": len(legacy_fixes),
        },
        "reports": agent_reports,
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
            "report": report_to_markdown(schema_dict, "Schema Integrity Validator"),
            "json_report": schema_dict,
        },
        "statistical_analysis": {
            **statistical_dict,
            "report": report_to_markdown(statistical_dict, "Statistical Validator"),
            "json_report": statistical_dict,
        },
        "structural_analysis": {
            **structural_dict,
            "report": report_to_markdown(structural_dict, "Structural Validator"),
            "json_report": structural_dict,
        },
        "relational_analysis": {
            **relational_dict,
            "report": report_to_markdown(relational_dict, "Relational Validator"),
            "json_report": relational_dict,
        },
        "profiler_tables_count": len(profiler_report.tables),
        "fixes": {
            "fixes": legacy_fixes,
        },
        "agent_reports": agent_reports,
        "final_report": fix_dict,
        "download_report_json": download_report_bundle,
        "confidence_score": confidence_score,
        "data_quality_score": data_quality_score,
        **report_artifact,
    }
