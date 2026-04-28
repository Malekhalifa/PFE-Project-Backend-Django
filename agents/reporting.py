"""
reporting.py - Minimalistic Validation Reporting

This drops the complex parsing back-and-forth from the legacy
reporting.py. Since the new validation.py relies on Pydantic and native Agno structured 
outputs, this service only handles aggregating standard dictionaries. here (markdown conversion and scoring).
"""

import json
from typing import Any

import hashlib
import re
from urllib.parse import urlparse

def derive_database_id(connection_string: str) -> str:
    try:
        parsed = urlparse(connection_string)
        host = parsed.hostname or "unknown"
        port = str(parsed.port or 5432)
        dbname = (parsed.path or "/unknown").lstrip("/") or "unknown"
        raw = f"{host}_{port}_{dbname}"
        safe = re.sub(r"[^a-zA-Z0-9._-]", "_", raw)
        return safe
    except Exception:
        stripped = re.sub(r"://[^@]+@", "://***@", connection_string)
        return hashlib.md5(stripped.encode()).hexdigest()[:16]


# ---------------------------------------------------------------------------
# Formatting Helpers
# ---------------------------------------------------------------------------

def _as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _as_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _as_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value) if value is not None else default
    except Exception:
        return default


def normalize_severity(value: Any) -> str:
    s = str(value or "").strip().lower()
    if s in {"critical", "high", "medium", "low"}:
        return s
    if s in {"warn", "warning", "minor"}:
        return "low"
    if s in {"major"}:
        return "high"
    if s in {"blocker"}:
        return "critical"
    return "medium"


def _severity_label(value: Any) -> str:
    return normalize_severity(value).upper()


def _humanize_metric_key(key: str) -> str:
    return str(key or "").replace("_", " ").strip().capitalize()


def _truncate_for_markdown(value: Any, *, max_chars: int = 320) -> str:
    text = str(value or "").replace("\n", " ").strip()
    if len(text) <= max_chars:
        return text
    limit = max_chars - 3
    truncated = text[:limit]
    return f"{truncated.rstrip()}..."


def _pick_table_name(item: dict[str, Any]) -> str:
    for key in ("table", "table_name", "source_table", "target_table"):
        value = str(item.get(key) or "").strip()
        if value:
            return value
    return "unknown_table"


def _extract_item_issues(item: dict[str, Any]) -> list[dict[str, Any]]:
    if isinstance(item.get("issues"), list):
        return [dict(i) for i in item["issues"] if isinstance(i, dict)]
    return []


def _resolve_primary_issue(finding: dict[str, Any]) -> dict[str, Any]:
    issues = _extract_item_issues(finding)
    if issues:
        return issues[0]
    return {
        "severity": normalize_severity(finding.get("severity")),
        "description": finding.get("description") or finding.get("problem") or "Issue detected",
    }


def _format_evidence_lines(evidence: dict[str, Any]) -> list[str]:
    evidence_dict = _as_dict(evidence)
    if not evidence_dict:
        return []

    lines = []
    query = evidence_dict.get("query")
    if query:
        lines.append(f"  - Query: `{_truncate_for_markdown(query, max_chars=500)}`")

    row_count = evidence_dict.get("row_count")
    if row_count is not None:
        lines.append(f"  - Row count: {row_count}")

    sample = evidence_dict.get("sample")
    if sample not in (None, "", [], {}):
        try:
            serialized = json.dumps(sample, ensure_ascii=False)
        except Exception:
            serialized = str(sample)
        lines.append(f"  - Sample: `{_truncate_for_markdown(serialized, max_chars=500)}`")

    return lines


# ---------------------------------------------------------------------------
# Public Reporting Presentation API
# ---------------------------------------------------------------------------

def aggregate_pipeline_scores(reports: list[dict[str, Any]]) -> tuple[int, int]:
    """
    Computes an overall combined confidence score and data quality score.
    """
    summaries = [
        _as_dict(report.get("summary")) 
        for report in reports if isinstance(report, dict)
    ]
    if not summaries:
        return 0, 0

    confidence_values = [max(0.0, min(1.0, _as_float(s.get("overall_confidence"), 0.0))) for s in summaries]
    quality_values = [max(0.0, min(100.0, _as_float(s.get("overall_quality_score"), 0.0))) for s in summaries]

    confidence_score = int(round((sum(confidence_values) / len(confidence_values)) * 100.0))
    data_quality_score = int(round(sum(quality_values) / len(quality_values)))

    return confidence_score, data_quality_score


def report_to_markdown(report: dict[str, Any], title: str) -> str:
    """
    Converts a Pydantic-dictified pipeline report into user-friendly Markdown.
    """
    summary = _as_dict(report.get("summary"))
    metrics = _as_dict(report.get("metrics"))
    findings = [f for f in _as_list(report.get("findings")) if isinstance(f, dict)]
    raw_dsl_signals = _as_list(report.get("raw_dsl_signals"))
    status_value = str(report.get("status") or "completed").strip().lower()
    flagged_tables = [str(t) for t in _as_list(summary.get("flagged_tables")) if str(t).strip()]
    errors = [str(e).strip() for e in _as_list(report.get("errors")) if str(e).strip()]

    lines = [
        f"## {title}",
        "",
        "### Executive Summary",
        f"- Status: {status_value}",
        f"- Total analyzed: {summary.get('total_items_analyzed', 0)}",
        f"- Passed: {summary.get('items_passed', 0)}",
        f"- Flagged: {summary.get('items_flagged', 0)}",
        (
            "- Severity split: "
            f"critical={summary.get('critical_issues', 0)}, "
            f"high={summary.get('high_issues', 0)}, "
            f"medium={summary.get('medium_issues', 0)}, "
            f"low={summary.get('low_issues', 0)}"
        ),
        f"- Quality score: {summary.get('overall_quality_score', 0)} / 100",
        f"- Confidence: {summary.get('overall_confidence', 0)}",
    ]

    if flagged_tables:
        lines.append(f"- Flagged tables: {', '.join(sorted(set(flagged_tables)))}")

    if errors:
        lines.append("\n### Partial-Run Notes")
        for err in errors:
            lines.append(f"- {err}")

    if metrics:
        lines.append("\n### Metrics")
        for key in sorted(metrics.keys()):
            lines.append(f"- {_humanize_metric_key(key)}: {metrics.get(key)}")

    if findings:
        lines.append(f"\n### Findings ({len(findings)})")
        for idx, finding in enumerate(findings, start=1):
            table_name = _pick_table_name(finding)
            issues = _extract_item_issues(finding)
            primary_issue = _resolve_primary_issue(finding)
            severity = _severity_label(primary_issue.get("severity"))
            description = primary_issue.get("description") or primary_issue.get("type") or "Issue detected"
            recommendation = (
                finding.get("remediation_hint")
                or primary_issue.get("remediation")
                or finding.get("fix")
                or "Review this item and apply the safest corrective action."
            )

            lines.append(f"\n#### {idx}. {table_name} [{severity}]")
            lines.append(f"- Problem: {description}")

            if "confidence" in finding:
                lines.append(f"- Confidence: {finding['confidence']}")
            if "column" in finding:
                lines.append(f"- Column: {finding['column']}")
            if "row_id" in finding or "row_identifier" in finding:
                lines.append(f"- Row identifier: {finding.get('row_id') or finding.get('row_identifier')}")

            lines.append(f"- Recommendation: {recommendation}")

            if issues:
                lines.append("- Detailed issues:")
                for issue in issues:
                    issue_sev = _severity_label(issue.get("severity"))
                    issue_desc = issue.get("description") or issue.get("type") or "Issue detected"
                    lines.append(f"  - [{issue_sev}] {issue_desc}")

            evidence = _as_dict(primary_issue.get("evidence") or finding.get("evidence"))
            evidence_lines = _format_evidence_lines(evidence)
            if evidence_lines:
                lines.append("- Evidence:")
                lines.extend(evidence_lines)

            sql_queries = [str(q).strip() for q in _as_list(finding.get("queries")) if str(q).strip()]
            if sql_queries:
                lines.append("- Proposed SQL:")
                for query in sql_queries:
                    lines.append(f"```sql\n{query}\n```")

            explanation = str(finding.get("explanation") or "").strip()
            if explanation:
                lines.append(f"- Explanation: {_truncate_for_markdown(explanation, max_chars=500)}")
    elif raw_dsl_signals:
        lines.append(f"\n### Reduced DSL Signals ({len(raw_dsl_signals)})")
        for sig in raw_dsl_signals:
            lines.append(f"- `{sig}`")
    else:
        lines.append("\nNo issues were found by this agent.")

    return "\n".join(lines)
