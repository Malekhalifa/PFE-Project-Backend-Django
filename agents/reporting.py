import json
import re
import ast
from datetime import datetime, timezone
from typing import Any, Iterable

from .guidelines_store import _derive_database_id

SEVERITY_WEIGHTS = {
    "critical": 40,
    "high": 20,
    "medium": 10,
    "low": 3,
}


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def derive_database_id(connection_string: str) -> str:
    return _derive_database_id(connection_string)


def clone_json(value: Any) -> Any:
    return json.loads(json.dumps(value, ensure_ascii=False))


def strip_code_fences(raw_text: str) -> str:
    payload = (raw_text or "").strip()
    if payload.startswith("```"):
        payload = payload.split("\n", 1)[-1]
        payload = payload.rsplit("```", 1)[0].strip()
    return payload


def parse_json_payload(raw_text: str) -> tuple[dict[str, Any] | list[Any] | None, str | None]:
    payload = strip_code_fences(raw_text)
    if not payload:
        return None, "empty_output"

    def _try_parse(candidate: str) -> tuple[dict[str, Any] | list[Any] | None, str | None]:
        try:
            parsed = json.loads(candidate)
            if isinstance(parsed, (dict, list)):
                return parsed, None
            return None, "json_root_must_be_object_or_array"
        except Exception as exc:
            return None, str(exc)

    def _extract_balanced_json(candidate: str) -> str | None:
        start = -1
        opener = ""
        for i, ch in enumerate(candidate):
            if ch in "[{":
                start = i
                opener = ch
                break
        if start < 0:
            return None

        closer = "]" if opener == "[" else "}"
        depth = 0
        in_string = False
        escaped = False

        for i in range(start, len(candidate)):
            ch = candidate[i]
            if in_string:
                if escaped:
                    escaped = False
                    continue
                if ch == "\\":
                    escaped = True
                    continue
                if ch == '"':
                    in_string = False
                continue

            if ch == '"':
                in_string = True
                continue

            if ch == opener:
                depth += 1
            elif ch == closer:
                depth -= 1
                if depth == 0:
                    return candidate[start:i + 1]

        return None

    parsed, parse_error = _try_parse(payload)
    if parsed is not None:
        return parsed, None

    extracted = _extract_balanced_json(payload)
    if extracted:
        parsed, extracted_error = _try_parse(extracted)
        if parsed is not None:
            return parsed, None

        # Common LLM issue: trailing commas before ] or }
        sanitized = re.sub(r",\s*([}\]])", r"\1", extracted)
        parsed, sanitized_error = _try_parse(sanitized)
        if parsed is not None:
            return parsed, None

        # Last-resort recovery for Python-literal-like payloads.
        try:
            literal = ast.literal_eval(sanitized)
            if isinstance(literal, (dict, list)):
                return literal, None
        except Exception:
            pass

        return None, sanitized_error or extracted_error or parse_error

    try:
        parsed = json.loads(payload)
        if isinstance(parsed, (dict, list)):
            return parsed, None
        return None, "json_root_must_be_object_or_array"
    except Exception as exc:
        return None, str(exc)


def normalize_severity(value: Any) -> str:
    s = str(value or "").strip().lower()
    if s in SEVERITY_WEIGHTS:
        return s
    if s in {"warn", "warning", "minor"}:
        return "low"
    if s in {"major"}:
        return "high"
    if s in {"blocker"}:
        return "critical"
    return "medium"


def _as_int(value: Any, default: int = 0) -> int:
    try:
        if value is None:
            return default
        return int(value)
    except Exception:
        return default


def _as_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None:
            return default
        return float(value)
    except Exception:
        return default


def _as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _as_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _pick_table_name(item: dict[str, Any]) -> str:
    for key in ("table", "table_name", "source_table", "target_table"):
        value = str(item.get(key) or "").strip()
        if value:
            return value
    return ""


def _pick_item_weight(item: dict[str, Any]) -> float:
    for key in (
        "item_weight",
        "weight",
        "row_count",
        "column_count",
        "size",
        "sample_size",
        "rows_analyzed",
        "pairs_analyzed",
    ):
        value = item.get(key)
        if value is not None:
            parsed = _as_float(value, 1.0)
            if parsed > 0:
                return parsed
    return 1.0


def _pick_item_confidence(item: dict[str, Any], default: float = 0.75) -> float:
    value = item.get("confidence")
    if isinstance(value, (int, float)):
        return max(0.0, min(1.0, float(value)))

    issues = _extract_item_issues(item)
    for issue in issues:
        issue_conf = issue.get("confidence")
        if isinstance(issue_conf, (int, float)):
            return max(0.0, min(1.0, float(issue_conf)))

    return default


def _extract_item_issues(item: dict[str, Any]) -> list[dict[str, Any]]:
    if isinstance(item.get("issues"), list):
        issues: list[dict[str, Any]] = []
        for issue in item["issues"]:
            issue_dict = _as_dict(issue)
            if not issue_dict:
                continue
            issue_dict = dict(issue_dict)
            issue_dict["severity"] = normalize_severity(issue_dict.get("severity"))
            issues.append(issue_dict)
        return issues

    # Fallback: treat the finding itself as one issue if a severity is present.
    if item.get("severity") is not None:
        issue = {
            "type": item.get("type") or "issue",
            "description": item.get("description") or item.get("problem") or "Issue detected",
            "severity": normalize_severity(item.get("severity")),
        }
        return [issue]

    return []


def _issue_text(issue: dict[str, Any]) -> str:
    parts = [
        str(issue.get("type") or ""),
        str(issue.get("category") or ""),
        str(issue.get("description") or ""),
    ]
    return " ".join(parts).strip().lower()


def _iter_issues(findings: list[dict[str, Any]]) -> Iterable[tuple[dict[str, Any], dict[str, Any], str]]:
    for item in findings:
        issues = _extract_item_issues(item)
        for issue in issues:
            yield item, issue, _issue_text(issue)


def _item_score_from_issues(issues: list[dict[str, Any]]) -> float:
    deduction = 0
    for issue in issues:
        deduction += SEVERITY_WEIGHTS[normalize_severity(issue.get("severity"))]
    return max(0.0, 100.0 - float(deduction))


def _flagged_item(item: dict[str, Any]) -> bool:
    issues = _extract_item_issues(item)
    if issues:
        return True
    status = str(item.get("status") or "").strip().lower()
    return status in {"invalid", "flagged", "fail", "failed", "error"}


def _compute_summary(
    findings: list[dict[str, Any]],
    total_items_analyzed: int,
    default_confidence: float = 0.75,
) -> dict[str, Any]:
    clean_findings = [f for f in findings if isinstance(f, dict)]
    flagged = [f for f in clean_findings if _flagged_item(f)]

    severity_counts = {"critical": 0, "high": 0, "medium": 0, "low": 0}
    flagged_tables: set[str] = set()
    weighted_score_sum = 0.0
    total_weight = 0.0
    confidences: list[float] = []

    for item in clean_findings:
        issues = _extract_item_issues(item)
        if issues:
            for issue in issues:
                sev = normalize_severity(issue.get("severity"))
                severity_counts[sev] += 1

        score = _item_score_from_issues(issues)
        weight = _pick_item_weight(item)
        weighted_score_sum += score * weight
        total_weight += weight

        confidences.append(_pick_item_confidence(item, default=default_confidence))

        if issues:
            table_name = _pick_table_name(item)
            if table_name:
                flagged_tables.add(table_name)

    item_count = max(total_items_analyzed, len(clean_findings))
    extra_passed = max(item_count - len(clean_findings), 0)
    if extra_passed > 0:
        weighted_score_sum += float(extra_passed) * 100.0
        total_weight += float(extra_passed)
        confidences.extend([default_confidence] * extra_passed)

    overall_quality_score = 100.0 if total_weight <= 0 else (weighted_score_sum / total_weight)
    overall_confidence = default_confidence if not confidences else (sum(confidences) / float(len(confidences)))

    flagged_count = len(flagged)
    passed_count = max(item_count - flagged_count, 0)

    return {
        "total_items_analyzed": int(item_count),
        "items_passed": int(passed_count),
        "items_flagged": int(flagged_count),
        "critical_issues": int(severity_counts["critical"]),
        "high_issues": int(severity_counts["high"]),
        "medium_issues": int(severity_counts["medium"]),
        "low_issues": int(severity_counts["low"]),
        "overall_quality_score": round(max(0.0, min(100.0, overall_quality_score)), 2),
        "overall_confidence": round(max(0.0, min(1.0, overall_confidence)), 4),
        "flagged_tables": sorted(flagged_tables),
    }


def _count_issue_matches(
    findings: list[dict[str, Any]],
    keywords: tuple[str, ...],
    unique_table: bool = False,
) -> int:
    if unique_table:
        tables: set[str] = set()
        for item, _, text in _iter_issues(findings):
            if all(word in text for word in keywords):
                table_name = _pick_table_name(item)
                if table_name:
                    tables.add(table_name)
        return len(tables)

    count = 0
    for _, _, text in _iter_issues(findings):
        if all(word in text for word in keywords):
            count += 1
    return count


def _coalesce_int(default: int, *values: Any) -> int:
    for value in values:
        if value is None:
            continue
        parsed = _as_int(value, default)
        if parsed != default:
            return parsed
    return default


def _coalesce_float(default: float, *values: Any) -> float:
    for value in values:
        if value is None:
            continue
        parsed = _as_float(value, default)
        if parsed != default:
            return parsed
    return default


def _sanitize_findings(raw_findings: Any) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    for item in _as_list(raw_findings):
        item_dict = _as_dict(item)
        if item_dict:
            findings.append(item_dict)
    return findings


def _normalize_column_findings(payload: dict[str, Any]) -> list[dict[str, Any]]:
    if isinstance(payload.get("findings"), list):
        return _sanitize_findings(payload.get("findings"))

    findings: list[dict[str, Any]] = []
    for table_entry in _as_list(payload.get("tables")):
        table_dict = _as_dict(table_entry)
        if not table_dict:
            continue
        table_name = str(table_dict.get("table") or table_dict.get("table_name") or "").strip()
        for column_entry in _as_list(table_dict.get("columns")):
            col_dict = _as_dict(column_entry)
            column_name = str(col_dict.get("column") or col_dict.get("name") or "").strip()
            findings.append(
                {
                    "table": table_name,
                    "column": column_name,
                    "item_id": f"{table_name}.{column_name}" if table_name else column_name,
                    "status": col_dict.get("status"),
                    "quality_score": col_dict.get("quality_score"),
                    "confidence": col_dict.get("confidence"),
                    "metrics": _as_dict(col_dict.get("metrics")),
                    "issues": _as_list(col_dict.get("issues")),
                    "suggested_fixes": _as_list(col_dict.get("suggested_fixes")),
                }
            )
    return findings


def _collect_column_metric_components(findings: list[dict[str, Any]]) -> tuple[dict[str, int], float, float]:
    quality_values: list[float] = []
    confidence_values: list[float] = []

    counters = {
        "duplicate_columns_found": 0,
        "constant_columns_found": 0,
        "high_null_ratio_columns": 0,
        "low_cardinality_anomalies": 0,
        "high_cardinality_anomalies": 0,
        "semantic_mismatches": 0,
        "domain_violations": 0,
        "distribution_anomalies": 0,
    }

    for item in findings:
        item_metrics = _as_dict(item.get("metrics"))
        issues = _extract_item_issues(item)

        quality = item.get("quality_score")
        if isinstance(quality, (int, float)):
            quality_values.append(float(quality))
        else:
            quality_values.append(_item_score_from_issues(issues))

        confidence_values.append(_pick_item_confidence(item, default=0.78))

        issue_texts = " ".join(_issue_text(issue) for issue in issues)
        if "duplicate" in issue_texts:
            counters["duplicate_columns_found"] += 1
        if "constant" in issue_texts or _as_float(item_metrics.get("dominant_value_ratio"), 0.0) >= 0.95:
            counters["constant_columns_found"] += 1
        if _as_float(item_metrics.get("null_ratio"), 0.0) > 0.2:
            counters["high_null_ratio_columns"] += 1
        if "low_cardinality" in issue_texts:
            counters["low_cardinality_anomalies"] += 1
        if "high_cardinality" in issue_texts:
            counters["high_cardinality_anomalies"] += 1
        if "semantic" in issue_texts:
            counters["semantic_mismatches"] += 1
        if "domain" in issue_texts:
            counters["domain_violations"] += 1
        if any(token in issue_texts for token in ("distribution", "skew", "entropy", "cardinality_drift")):
            counters["distribution_anomalies"] += 1

    avg_quality = 100.0 if not quality_values else (sum(quality_values) / float(len(quality_values)))
    avg_confidence = 0.78 if not confidence_values else (sum(confidence_values) / float(len(confidence_values)))
    return counters, avg_quality, avg_confidence


def _build_column_metrics(
    total_columns: int,
    counters: dict[str, int],
    avg_quality: float,
    avg_confidence: float,
    metrics_input: dict[str, Any],
) -> dict[str, Any]:
    return {
        "total_columns_analyzed": int(total_columns),
        "duplicate_columns_found": int(_coalesce_int(counters["duplicate_columns_found"], metrics_input.get("duplicate_columns_found"), counters["duplicate_columns_found"])),
        "constant_columns_found": int(_coalesce_int(counters["constant_columns_found"], metrics_input.get("constant_columns_found"), counters["constant_columns_found"])),
        "high_null_ratio_columns": int(_coalesce_int(counters["high_null_ratio_columns"], metrics_input.get("high_null_ratio_columns"), counters["high_null_ratio_columns"])),
        "low_cardinality_anomalies": int(_coalesce_int(counters["low_cardinality_anomalies"], metrics_input.get("low_cardinality_anomalies"), counters["low_cardinality_anomalies"])),
        "high_cardinality_anomalies": int(_coalesce_int(counters["high_cardinality_anomalies"], metrics_input.get("high_cardinality_anomalies"), counters["high_cardinality_anomalies"])),
        "semantic_mismatches": int(_coalesce_int(counters["semantic_mismatches"], metrics_input.get("semantic_mismatches"), counters["semantic_mismatches"])),
        "domain_violations": int(_coalesce_int(counters["domain_violations"], metrics_input.get("domain_violations"), counters["domain_violations"])),
        "distribution_anomalies": int(_coalesce_int(counters["distribution_anomalies"], metrics_input.get("distribution_anomalies"), counters["distribution_anomalies"])),
        "avg_column_quality_score": round(_coalesce_float(avg_quality, metrics_input.get("avg_column_quality_score"), avg_quality), 2),
        "avg_column_confidence": round(_coalesce_float(avg_confidence, metrics_input.get("avg_column_confidence"), avg_confidence), 4),
    }


def _base_report(
    *,
    pipeline_run_id: str,
    database_id: str,
    agent_name: str,
    summary: dict[str, Any],
    metrics: dict[str, Any],
    findings: list[dict[str, Any]],
    prior_reports: list[dict[str, Any]],
    partial_errors: list[str] | None = None,
) -> dict[str, Any]:
    report = {
        "pipeline_run_id": pipeline_run_id,
        "database_id": database_id,
        "run_timestamp": utc_now_iso(),
        "agent": agent_name,
        "summary": summary,
        "metrics": metrics,
        "findings": findings,
        "prior_reports": clone_json(prior_reports),
    }
    if partial_errors:
        report["status"] = "partial"
        report["errors"] = partial_errors
    return report


def build_schema_report(
    raw_text: str,
    *,
    pipeline_run_id: str,
    database_id: str,
    prior_reports: list[dict[str, Any]],
    runtime_error: str | None = None,
) -> dict[str, Any]:
    parsed, parse_error = parse_json_payload(raw_text)
    payload = _as_dict(parsed)

    findings = _sanitize_findings(payload.get("findings"))
    metrics_input = _as_dict(payload.get("metrics"))
    summary_input = _as_dict(payload.get("summary"))

    total_tables = _coalesce_int(0, metrics_input.get("total_tables"), payload.get("total_tables"))
    total_columns = _coalesce_int(0, metrics_input.get("total_columns"), payload.get("total_columns"))

    tables_with_no_pk = _coalesce_int(
        0,
        metrics_input.get("tables_with_no_primary_key"),
        _count_issue_matches(findings, ("primary", "missing"), unique_table=True),
    )
    missing_fks = _coalesce_int(
        0,
        metrics_input.get("missing_foreign_keys"),
        _count_issue_matches(findings, ("foreign", "missing")),
    )
    constraint_violations = _coalesce_int(
        0,
        metrics_input.get("constraint_violations"),
        _count_issue_matches(findings, ("constraint",)),
    )
    type_mismatches = _coalesce_int(
        0,
        metrics_input.get("type_mismatches"),
        _count_issue_matches(findings, ("type", "mismatch")),
    )
    nullable_violations = _coalesce_int(
        0,
        metrics_input.get("nullable_violations"),
        _count_issue_matches(findings, ("nullable",)),
    )

    metrics = {
        "total_tables": int(total_tables),
        "total_columns": int(total_columns),
        "tables_with_no_primary_key": int(tables_with_no_pk),
        "missing_foreign_keys": int(missing_fks),
        "constraint_violations": int(constraint_violations),
        "type_mismatches": int(type_mismatches),
        "nullable_violations": int(nullable_violations),
    }

    total_items = _coalesce_int(
        len(findings),
        summary_input.get("total_items_analyzed"),
        metrics.get("total_tables"),
        len(findings),
    )
    summary = _compute_summary(findings, total_items_analyzed=total_items, default_confidence=0.72)

    partial_errors = [e for e in (runtime_error, parse_error) if e]

    return _base_report(
        pipeline_run_id=pipeline_run_id,
        database_id=database_id,
        agent_name="SchemaIntegrityValidator",
        summary=summary,
        metrics=metrics,
        findings=findings,
        prior_reports=prior_reports,
        partial_errors=partial_errors,
    )


def build_column_report(
    raw_text: str,
    *,
    pipeline_run_id: str,
    database_id: str,
    prior_reports: list[dict[str, Any]],
    runtime_error: str | None = None,
) -> dict[str, Any]:
    parsed, parse_error = parse_json_payload(raw_text)
    payload = _as_dict(parsed)

    findings = _normalize_column_findings(payload)
    metrics_input = _as_dict(payload.get("metrics"))
    summary_input = _as_dict(payload.get("summary"))

    total_columns = _coalesce_int(
        len(findings),
        metrics_input.get("total_columns_analyzed"),
        summary_input.get("total_columns_analyzed"),
        len(findings),
    )

    counters, avg_quality, avg_confidence = _collect_column_metric_components(findings)
    metrics = _build_column_metrics(total_columns, counters, avg_quality, avg_confidence, metrics_input)

    summary = _compute_summary(findings, total_items_analyzed=total_columns, default_confidence=metrics["avg_column_confidence"])

    partial_errors = [e for e in (runtime_error, parse_error) if e]
    if parse_error and "invalid_json_payload" not in partial_errors:
        partial_errors = ["invalid_json_payload", *partial_errors]

    return _base_report(
        pipeline_run_id=pipeline_run_id,
        database_id=database_id,
        agent_name="ColumnValidatorAgent",
        summary=summary,
        metrics=metrics,
        findings=findings,
        prior_reports=prior_reports,
        partial_errors=partial_errors,
    )


def build_row_report(
    raw_text: str,
    *,
    pipeline_run_id: str,
    database_id: str,
    prior_reports: list[dict[str, Any]],
    runtime_error: str | None = None,
) -> dict[str, Any]:
    parsed, parse_error = parse_json_payload(raw_text)
    payload = _as_dict(parsed)

    findings = _sanitize_findings(payload.get("findings"))
    metrics_input = _as_dict(payload.get("metrics"))
    summary_input = _as_dict(payload.get("summary"))

    total_rows = _coalesce_int(
        len(findings),
        metrics_input.get("total_rows_analyzed"),
        summary_input.get("total_items_analyzed"),
        len(findings),
    )

    rows_with_issues = len([f for f in findings if _flagged_item(f)])
    issue_strings = [text for _, _, text in _iter_issues(findings)]

    format_violations = len([text for text in issue_strings if any(k in text for k in ("format", "email", "phone", "url"))])
    encoding_issues = len([text for text in issue_strings if any(k in text for k in ("encoding", "utf", "trailing", "casing"))])
    hidden_null_count = len([text for text in issue_strings if any(k in text for k in ("hidden_null", "string_null", "placeholder_null", "n/a"))])
    outlier_rows = len([text for text in issue_strings if "outlier" in text])

    row_scores: list[float] = []
    row_confidences: list[float] = []
    for finding in findings:
        issues = _extract_item_issues(finding)
        row_scores.append(_as_float(finding.get("quality_score"), _item_score_from_issues(issues)))
        row_confidences.append(_pick_item_confidence(finding, default=0.73))

    avg_row_quality = 100.0 if not row_scores else (sum(row_scores) / float(len(row_scores)))
    avg_row_conf = 0.73 if not row_confidences else (sum(row_confidences) / float(len(row_confidences)))

    def _row_has_execution_evidence() -> bool:
        signal_keys = (
            "checks_executed",
            "rules_executed",
            "queries_executed",
            "tables_checked",
            "tables_scanned",
            "rules_checked",
        )
        for container in (payload, metrics_input, summary_input):
            if not isinstance(container, dict):
                continue
            for key in signal_keys:
                if _as_int(container.get(key), 0) > 0:
                    return True
            checks_value = container.get("checks")
            if isinstance(checks_value, list) and checks_value:
                return True
            evidence_queries = container.get("evidence_queries")
            if isinstance(evidence_queries, list) and evidence_queries:
                return True
        return False

    missing_row_evidence_marker: str | None = None
    avg_row_conf_metric = _coalesce_float(avg_row_conf, metrics_input.get("avg_row_confidence"), avg_row_conf)
    if total_rows > 0 and not findings and not _row_has_execution_evidence():
        # When no finding-level evidence exists, avoid reporting unwarranted certainty.
        avg_row_conf_metric = min(avg_row_conf_metric, 0.73)
        missing_row_evidence_marker = "row_validator_no_findings_or_execution_evidence"

    metrics = {
        "total_rows_analyzed": int(total_rows),
        "rows_with_issues": int(_coalesce_int(rows_with_issues, metrics_input.get("rows_with_issues"), rows_with_issues)),
        "rows_with_issues_ratio": round(float(_coalesce_float(
            (rows_with_issues / total_rows) if total_rows > 0 else 0.0,
            metrics_input.get("rows_with_issues_ratio"),
            (rows_with_issues / total_rows) if total_rows > 0 else 0.0,
        )), 4),
        "format_violations": int(_coalesce_int(format_violations, metrics_input.get("format_violations"), format_violations)),
        "encoding_issues": int(_coalesce_int(encoding_issues, metrics_input.get("encoding_issues"), encoding_issues)),
        "hidden_null_count": int(_coalesce_int(hidden_null_count, metrics_input.get("hidden_null_count"), hidden_null_count)),
        "outlier_rows": int(_coalesce_int(outlier_rows, metrics_input.get("outlier_rows"), outlier_rows)),
        "avg_row_quality_score": round(_coalesce_float(avg_row_quality, metrics_input.get("avg_row_quality_score"), avg_row_quality), 2),
        "avg_row_confidence": round(avg_row_conf_metric, 4),
    }

    summary = _compute_summary(findings, total_items_analyzed=total_rows, default_confidence=metrics["avg_row_confidence"])

    partial_errors = [e for e in (runtime_error, parse_error, missing_row_evidence_marker) if e]

    return _base_report(
        pipeline_run_id=pipeline_run_id,
        database_id=database_id,
        agent_name="RowLevelValidator",
        summary=summary,
        metrics=metrics,
        findings=findings,
        prior_reports=prior_reports,
        partial_errors=partial_errors,
    )


def build_cross_row_report(
    raw_text: str,
    *,
    pipeline_run_id: str,
    database_id: str,
    prior_reports: list[dict[str, Any]],
    runtime_error: str | None = None,
) -> dict[str, Any]:
    parsed, parse_error = parse_json_payload(raw_text)
    payload = _as_dict(parsed)

    findings = _sanitize_findings(payload.get("findings"))
    metrics_input = _as_dict(payload.get("metrics"))
    summary_input = _as_dict(payload.get("summary"))

    total_pairs = _coalesce_int(
        len(findings),
        metrics_input.get("total_row_pairs_analyzed"),
        summary_input.get("total_items_analyzed"),
        len(findings),
    )

    issue_strings = [text for _, _, text in _iter_issues(findings)]
    temporal_anomalies = len([text for text in issue_strings if any(k in text for k in ("temporal", "spike", "drop"))])
    sequential_breaks = len([text for text in issue_strings if any(k in text for k in ("sequence", "sequential", "gap", "reversal"))])
    cross_column_violations = len([text for text in issue_strings if any(k in text for k in ("cross_column", "end_date", "start_date", "logical"))])
    row_pattern_anomalies = len([text for text in issue_strings if any(k in text for k in ("row_pattern", "neighborhood", "surrounding"))])

    tables_with_issues = set()
    rel_confidences = []
    for finding in findings:
        if _flagged_item(finding):
            table_name = _pick_table_name(finding)
            if table_name:
                tables_with_issues.add(table_name)
        rel_confidences.append(_pick_item_confidence(finding, default=0.76))

    avg_rel_confidence = 0.76 if not rel_confidences else (sum(rel_confidences) / float(len(rel_confidences)))

    metrics = {
        "total_row_pairs_analyzed": int(total_pairs),
        "temporal_anomalies": int(_coalesce_int(temporal_anomalies, metrics_input.get("temporal_anomalies"), temporal_anomalies)),
        "sequential_breaks": int(_coalesce_int(sequential_breaks, metrics_input.get("sequential_breaks"), sequential_breaks)),
        "cross_column_violations": int(_coalesce_int(cross_column_violations, metrics_input.get("cross_column_violations"), cross_column_violations)),
        "row_pattern_anomalies": int(_coalesce_int(row_pattern_anomalies, metrics_input.get("row_pattern_anomalies"), row_pattern_anomalies)),
        "tables_with_cross_row_issues": int(_coalesce_int(len(tables_with_issues), metrics_input.get("tables_with_cross_row_issues"), len(tables_with_issues))),
        "avg_relationship_confidence": round(_coalesce_float(avg_rel_confidence, metrics_input.get("avg_relationship_confidence"), avg_rel_confidence), 4),
    }

    summary = _compute_summary(findings, total_items_analyzed=total_pairs, default_confidence=metrics["avg_relationship_confidence"])

    partial_errors = [e for e in (runtime_error, parse_error) if e]
    if parse_error and "invalid_json_payload" not in partial_errors:
        partial_errors = ["invalid_json_payload", *partial_errors]

    return _base_report(
        pipeline_run_id=pipeline_run_id,
        database_id=database_id,
        agent_name="CrossRowValidator",
        summary=summary,
        metrics=metrics,
        findings=findings,
        prior_reports=prior_reports,
        partial_errors=partial_errors,
    )


def _normalize_fix_findings(payload: dict[str, Any] | list[Any]) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [dict(item) for item in payload if isinstance(item, dict)]

    payload_dict = _as_dict(payload)
    if isinstance(payload_dict.get("findings"), list):
        return [dict(item) for item in payload_dict["findings"] if isinstance(item, dict)]
    if isinstance(payload_dict.get("fixes"), list):
        return [dict(item) for item in payload_dict["fixes"] if isinstance(item, dict)]
    return []


def synthesize_fixes_from_prior_reports(
    prior_reports: list[dict[str, Any]],
    *,
    max_fixes: int = 25,
) -> list[dict[str, Any]]:
    """
    Build fallback fix cards from prior validator findings.

    This is used when DataFixPlanner returns no structured findings even though
    upstream validators reported flagged issues.
    """
    synthesized: list[dict[str, Any]] = []
    dedupe_keys: set[str] = set()

    for report in _as_list(prior_reports):
        report_dict = _as_dict(report)
        agent_name = str(report_dict.get("agent") or "UnknownAgent")

        for finding_raw in _as_list(report_dict.get("findings")):
            finding = _as_dict(finding_raw)
            if not finding or not _flagged_item(finding):
                continue

            issues = _extract_item_issues(finding)
            first_issue = issues[0] if issues else {}

            severity = normalize_severity(first_issue.get("severity") or finding.get("severity"))
            table_name = _pick_table_name(finding) or "unknown_table"
            problem = str(
                first_issue.get("description")
                or finding.get("description")
                or finding.get("problem")
                or "Issue detected and requires remediation."
            ).strip()
            remediation = str(
                first_issue.get("remediation")
                or finding.get("remediation")
                or "Review the issue and prepare a safe SQL remediation query."
            ).strip()

            dedupe = f"{table_name}|{severity}|{problem.lower()}"
            if dedupe in dedupe_keys:
                continue
            dedupe_keys.add(dedupe)

            fix_id = f"AUTO_FIX_{len(synthesized) + 1:03d}"
            placeholder_sql = (
                "-- AUTO-GENERATED PLACEHOLDER. Replace this SQL with the real fix before applying.\n"
                f"-- Source agent: {agent_name}\n"
                f"-- Severity: {severity}\n"
                "INVALID_SQL_PLACEHOLDER;"
            )

            synthesized.append(
                {
                    "id": fix_id,
                    "table": table_name,
                    "target_table": table_name,
                    "problem": problem,
                    "fix": remediation,
                    "queries": [placeholder_sql],
                    "explanation": (
                        "Fallback fix card generated because DataFixPlanner returned no structured findings. "
                        "Edit SQL before applying."
                    ),
                    "severity": severity,
                    "source_agent": agent_name,
                }
            )

            if len(synthesized) >= max_fixes:
                return synthesized

    return synthesized


def build_fix_planner_report(
    raw_text: str,
    *,
    pipeline_run_id: str,
    database_id: str,
    prior_reports: list[dict[str, Any]],
    runtime_error: str | None = None,
) -> dict[str, Any]:
    parsed, parse_error = parse_json_payload(raw_text)
    payload = parsed if isinstance(parsed, (dict, list)) else {}

    findings = _normalize_fix_findings(payload)
    payload_dict = _as_dict(payload)
    metrics_input = _as_dict(payload_dict.get("metrics"))

    total_issues_received_from_summary = sum(
        _as_int(_as_dict(report.get("summary")).get("items_flagged"), 0)
        for report in prior_reports
        if isinstance(report, dict)
    )

    total_flagged_findings = 0
    for report in prior_reports:
        report_dict = _as_dict(report)
        for finding_raw in _as_list(report_dict.get("findings")):
            finding = _as_dict(finding_raw)
            if finding and _flagged_item(finding):
                total_flagged_findings += 1

    total_issues_received = max(total_issues_received_from_summary, total_flagged_findings)

    if not findings and total_issues_received > 0:
        findings = synthesize_fixes_from_prior_reports(prior_reports)
        if findings:
            if parse_error:
                parse_error = f"{parse_error}; fix_planner_returned_no_findings_using_fallback"
            else:
                parse_error = "fix_planner_returned_no_findings_using_fallback"

    fixes_generated = len(findings)
    critical_fixes = 0
    high_fixes = 0
    medium_fixes = 0
    low_fixes = 0
    estimated_rows_affected = 0
    fix_confidences: list[float] = []

    fix_items_for_summary = []

    for fix in findings:
        sev = normalize_severity(fix.get("severity"))
        if sev == "critical":
            critical_fixes += 1
        elif sev == "high":
            high_fixes += 1
        elif sev == "low":
            low_fixes += 1
        else:
            medium_fixes += 1

        if isinstance(fix.get("estimated_rows_affected"), (int, float)):
            estimated_rows_affected += int(fix["estimated_rows_affected"])
        elif isinstance(fix.get("rows_affected"), list):
            estimated_rows_affected += sum(_as_int(x, 0) for x in fix["rows_affected"])

        fix_confidences.append(_pick_item_confidence(fix, default=0.8))

        fix_items_for_summary.append(
            {
                "table": fix.get("table"),
                "confidence": _pick_item_confidence(fix, default=0.8),
                "issues": [
                    {
                        "type": "fix_planned",
                        "description": fix.get("problem") or "Issue fix planned",
                        "severity": sev,
                    }
                ],
            }
        )

    fix_confidence = 0.8 if not fix_confidences else (sum(fix_confidences) / float(len(fix_confidences)))

    metrics = {
        "total_issues_received": int(max(total_issues_received, _as_int(metrics_input.get("total_issues_received"), 0))),
        "fixes_generated": int(max(fixes_generated, _as_int(metrics_input.get("fixes_generated"), 0))),
        "critical_fixes": int(max(critical_fixes, _as_int(metrics_input.get("critical_fixes"), 0))),
        "high_fixes": int(max(high_fixes, _as_int(metrics_input.get("high_fixes"), 0))),
        "medium_fixes": int(max(medium_fixes, _as_int(metrics_input.get("medium_fixes"), 0))),
        "low_fixes": int(max(low_fixes, _as_int(metrics_input.get("low_fixes"), 0))),
        "estimated_rows_affected": int(max(estimated_rows_affected, _as_int(metrics_input.get("estimated_rows_affected"), 0))),
        "fix_confidence": round(_coalesce_float(fix_confidence, metrics_input.get("fix_confidence"), fix_confidence), 4),
    }

    total_items = max(metrics["total_issues_received"], metrics["fixes_generated"])
    summary = _compute_summary(fix_items_for_summary, total_items_analyzed=total_items, default_confidence=metrics["fix_confidence"])

    # Planner summary is issue-driven.
    summary["total_items_analyzed"] = int(total_items)
    summary["items_flagged"] = int(metrics["fixes_generated"])
    summary["items_passed"] = int(max(total_items - metrics["fixes_generated"], 0))
    summary["critical_issues"] = int(metrics["critical_fixes"])
    summary["high_issues"] = int(metrics["high_fixes"])
    summary["medium_issues"] = int(metrics["medium_fixes"])
    summary["low_issues"] = int(metrics["low_fixes"])
    summary["overall_confidence"] = round(metrics["fix_confidence"], 4)

    partial_errors = [e for e in (runtime_error, parse_error) if e]

    report = _base_report(
        pipeline_run_id=pipeline_run_id,
        database_id=database_id,
        agent_name="DataFixPlanner",
        summary=summary,
        metrics=metrics,
        findings=findings,
        prior_reports=prior_reports,
        partial_errors=partial_errors,
    )

    return report


def _humanize_metric_key(key: str) -> str:
    return str(key or "").replace("_", " ").strip().capitalize()


def _truncate_for_markdown(value: Any, *, max_chars: int = 320) -> str:
    text = str(value or "").replace("\n", " ").strip()
    if len(text) <= max_chars:
        return text
    return f"{text[:max_chars - 3].rstrip()}..."


def _format_evidence_lines(evidence: dict[str, Any]) -> list[str]:
    evidence_dict = _as_dict(evidence)
    if not evidence_dict:
        return []

    lines: list[str] = []

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


def _resolve_primary_issue(finding: dict[str, Any]) -> dict[str, Any]:
    issues = _extract_item_issues(finding)
    if issues:
        return issues[0]
    return {
        "severity": normalize_severity(finding.get("severity")),
        "description": finding.get("description") or finding.get("problem") or "Issue detected",
    }


def _severity_label(value: Any) -> str:
    return normalize_severity(value).upper()


def report_to_markdown(report: dict[str, Any], title: str) -> str:
    summary = _as_dict(report.get("summary"))
    metrics = _as_dict(report.get("metrics"))
    findings = [f for f in _as_list(report.get("findings")) if isinstance(f, dict)]
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
        lines.append("")
        lines.append("### Partial-Run Notes")
        for err in errors:
            lines.append(f"- {err}")

    if metrics:
        lines.append("")
        lines.append("### Metrics")
        for key in sorted(metrics.keys()):
            lines.append(f"- {_humanize_metric_key(key)}: {metrics.get(key)}")

    if findings:
        lines.append("")
        lines.append(f"### Findings ({len(findings)})")
        for idx, finding in enumerate(findings, start=1):
            table_name = _pick_table_name(finding) or "unknown_table"
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

            lines.append("")
            lines.append(f"#### {idx}. {table_name} [{severity}]")
            lines.append(f"- Problem: {description}")

            confidence = finding.get("confidence")
            if confidence is not None:
                lines.append(f"- Confidence: {confidence}")

            column_name = finding.get("column")
            if column_name:
                lines.append(f"- Column: {column_name}")

            row_identifier = finding.get("row_id") or finding.get("row_identifier")
            if row_identifier:
                lines.append(f"- Row identifier: {row_identifier}")

            lines.append(f"- Recommendation: {recommendation}")

            if issues:
                lines.append("- Detailed issues:")
                for issue in issues:
                    issue_sev = _severity_label(issue.get("severity"))
                    issue_desc = issue.get("description") or issue.get("type") or "Issue detected"
                    lines.append(f"  - [{issue_sev}] {issue_desc}")

            evidence = _as_dict(primary_issue.get("evidence"))
            if not evidence:
                evidence = _as_dict(finding.get("evidence"))
            evidence_lines = _format_evidence_lines(evidence)
            if evidence_lines:
                lines.append("- Evidence:")
                lines.extend(evidence_lines)

            sql_queries = [str(q).strip() for q in _as_list(finding.get("queries")) if str(q).strip()]
            if sql_queries:
                lines.append("- Proposed SQL:")
                for query in sql_queries:
                    lines.append("```sql")
                    lines.append(query)
                    lines.append("```")

            explanation = str(finding.get("explanation") or "").strip()
            if explanation:
                lines.append(f"- Explanation: {_truncate_for_markdown(explanation, max_chars=500)}")
    else:
        lines.append("")
        lines.append("No issues were found by this agent.")

    return "\n".join(lines)


def report_to_context_block(report: dict[str, Any], heading: str) -> str:
    serialized = json.dumps(report, ensure_ascii=False, indent=2)
    return f"## {heading}\n\n```json\n{serialized}\n```"


def aggregate_pipeline_scores(reports: list[dict[str, Any]]) -> tuple[int, int]:
    summaries = [
        _as_dict(report.get("summary"))
        for report in reports
        if isinstance(report, dict)
    ]
    if not summaries:
        return 0, 0

    confidence_values = [
        max(0.0, min(1.0, _as_float(summary.get("overall_confidence"), 0.0)))
        for summary in summaries
    ]
    quality_values = [
        max(0.0, min(100.0, _as_float(summary.get("overall_quality_score"), 0.0)))
        for summary in summaries
    ]

    confidence_score = int(round((sum(confidence_values) / float(len(confidence_values))) * 100.0))
    data_quality_score = int(round(sum(quality_values) / float(len(quality_values))))

    return confidence_score, data_quality_score


def safe_file_component(value: str) -> str:
    return re.sub(r"[^a-zA-Z0-9._-]", "_", str(value or "unknown")).strip("_") or "unknown"
