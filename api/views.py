"""
API views for jobs, analysis, and raw data.

Every endpoint matches the Angular frontend's expected API surface.

All views require authentication (CookieJWTAuthentication) and
enforce job ownership via find_owned_job().
"""
import math
import os
import uuid
import logging
import json
import re
import requests as http_requests
from datetime import datetime, time, timedelta, timezone
from typing import Any

from django.conf import settings
from django.core.mail import send_mail
from django.db.models import Count, Q
from django.http import FileResponse
from django.utils import timezone as dj_tz
from django.utils.dateparse import parse_date, parse_datetime
from rest_framework import status

from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.throttling import UserRateThrottle
from rest_framework.views import APIView

from config.celery import app as celery_app
from api.models import AnalysisResult, AuditLog, Job, audit_log
from api.db import (
    create_job,
    get_job,
    get_jobs_for_user,
    get_analysis_result,
    save_analysis_result,
    get_analysis_job_ids,
    get_chat_job_ids,
    get_chat_messages,
    save_chat_message,
    delete_jobs,
    update_job_connection_string,
    update_job_status,
    update_job_celery_task_id,
    get_job_guidelines,
)
from api.permissions import find_owned_job
from api.serializers import (
    AnalyzePostgresSerializer,
    ChatMessageCreateSerializer,
    JobDeleteSerializer,
)
from api.fix_safety import validate_fix_queries
from agents.query_executor_agent import QueryExecutorAgent
from services.notification_service import notify_user_fix_executed, notify_user_job_cancelled
from services.postgres_connection import (
    looks_like_postgres_dsn,
    redact_postgres_dsn,
    test_postgres_connection,
)
from tasks.postgres_tasks import analyze_postgres_task
from users.models import User


logger = logging.getLogger(__name__)


class AnalyzePostgresUserRateThrottle(UserRateThrottle):
    scope = 'analyze_postgres'


class ExecuteFixUserRateThrottle(UserRateThrottle):
    scope = 'execute_fix'


def _email_not_verified_response() -> Response:
    return Response(
        {
            'error': 'Please verify your email before running this action.',
            'code': 'email_not_verified',
        },
        status=status.HTTP_403_FORBIDDEN,
    )


def _require_verified_email(request) -> Response | None:
    if bool(getattr(request.user, 'email_verified', False)):
        return None
    return _email_not_verified_response()


def _sanitize_for_json(obj):
    """
    Recursively sanitize data for JSON serialization.
    - Replace NaN/Infinity floats with None (invalid in JSON)
    - Convert datetime objects to ISO strings
    """
    if isinstance(obj, dict):
        return {k: _sanitize_for_json(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_sanitize_for_json(v) for v in obj]
    if isinstance(obj, float):
        if math.isnan(obj) or math.isinf(obj):
            return None
    return obj


_ADMIN_ACTIVE_JOB_STATUSES = {'starting', 'running'}
_TERMINAL_ACTIVE_PREFIX_EXCLUSIONS: set[str] = set()
_PIPELINE_STEP_DEFINITIONS = [
    ('running_schema',      'Schema Integrity Validator'),
    ('running_profiler',    'Database Profiler'),
    ('running_statistical', 'Statistical Validator'),
    ('running_structural',  'Structural Validator'),
    ('running_relational',  'Relational Validator'),
    ('running_fix_planner', 'Data Fix Planner'),
]


def _require_admin_staff(request) -> Response | None:
    if bool(getattr(request.user, 'is_staff', False)):
        return None

    return Response(
        {
            'error': 'Admin access required.',
            'code': 'admin_required',
        },
        status=status.HTTP_403_FORBIDDEN,
    )


def _coerce_int(value, default: int, *, minimum: int | None = None, maximum: int | None = None) -> int:
    try:
        parsed = int(str(value).strip())
    except (TypeError, ValueError):
        parsed = default

    if minimum is not None and parsed < minimum:
        parsed = minimum
    if maximum is not None and parsed > maximum:
        parsed = maximum
    return parsed


def _parse_datetime_filter(value: str | None, *, end_of_day: bool = False):
    if not value:
        return None

    raw = str(value).strip()
    if not raw:
        return None

    parsed = parse_datetime(raw)
    if parsed is None:
        date_value = parse_date(raw)
        if date_value is None:
            return None
        parsed = datetime.combine(date_value, time.max if end_of_day else time.min)

    if dj_tz.is_naive(parsed):
        parsed = parsed.replace(tzinfo=timezone.utc)

    return parsed


def _extract_status_code(payload: Any) -> int | None:
    if not isinstance(payload, dict):
        return None

    raw = payload.get('status_code')
    if raw is None:
        return None

    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def _classify_audit_event(event_type: str, payload: Any) -> str | None:
    status_code = _extract_status_code(payload)
    if status_code is not None:
        if status_code >= 500:
            return 'error'
        if 200 <= status_code < 500:
            return 'success'

    event = str(event_type or '').upper()
    if any(token in event for token in ('FAILED', 'ERROR', 'EXCEPTION')):
        return 'error'
    if any(token in event for token in ('COMPLETED', 'SUCCESS', 'EXECUTED', 'SENT')):
        return 'success'
    return None


def _is_active_job_status(status_value: str | None) -> bool:
    normalized = str(status_value or '').strip().lower()
    if not normalized:
        return False

    if normalized in _ADMIN_ACTIVE_JOB_STATUSES:
        return True

    if normalized in _TERMINAL_ACTIVE_PREFIX_EXCLUSIONS:
        return False

    return normalized.startswith('running_')


_GUIDELINE_KEYS = [
    'schema_integrity_validator',
    'statistical_validator',
    'structural_validator',
    'relational_validator',
    'data_fix_planner',
]


def _default_column_validator_analysis() -> dict:
    return {
        'agent': 'ColumnValidatorAgent',
        'database_id': 'unknown',
        'run_timestamp': 'unknown',
        'report': 'No report generated.',
        'findings': [],
        'summary': {
            'total_items_analyzed': 0,
            'items_passed': 0,
            'items_flagged': 0,
            'critical_issues': 0,
            'high_issues': 0,
            'medium_issues': 0,
            'low_issues': 0,
            'overall_quality_score': 100,
            'overall_confidence': 0.0,
            'flagged_tables': [],
        },
        'metrics': {
            'total_columns_analyzed': 0,
            'duplicate_columns_found': 0,
            'constant_columns_found': 0,
            'high_null_ratio_columns': 0,
            'low_cardinality_anomalies': 0,
            'high_cardinality_anomalies': 0,
            'semantic_mismatches': 0,
            'domain_violations': 0,
            'distribution_anomalies': 0,
            'avg_column_quality_score': 100,
            'avg_column_confidence': 0.0,
        },
        'prior_reports': [],
        'error': 'column_validator_missing',
        'message': 'Column validator output was missing for this run.',
    }


def _ensure_column_validator_result(job_id: str, analysis_doc: dict) -> dict:
    """Ensure postgres-agentic result payload always includes column_validator_analysis."""
    result = dict((analysis_doc or {}).get('result') or {})
    if 'schema_analysis' not in result:
        return result
    if 'column_validator_analysis' in result:
        return result

    result['column_validator_analysis'] = _default_column_validator_analysis()
    try:
        save_analysis_result(
            job_id=job_id,
            result=result,
            analysis_type=(analysis_doc or {}).get('analysis_type', 'postgres_agentic'),
        )
    except Exception:
        # Never fail the response path because of a backfill write.
        pass
    return result


def _count_fix_cards(fixes_payload: Any) -> int:
    if isinstance(fixes_payload, list):
        return len([item for item in fixes_payload if isinstance(item, dict)])
    if isinstance(fixes_payload, dict):
        nested = fixes_payload.get('fixes')
        if isinstance(nested, list):
            return len([item for item in nested if isinstance(item, dict)])
    return 0


def _ensure_fix_cards_result(job_id: str, result: dict, analysis_type: str = 'postgres_agentic') -> dict:
    """Ensure result payload exposes non-empty `fixes` cards for UI consumers."""
    payload = dict(result or {})
    if _count_fix_cards(payload.get('fixes')) > 0:
        return payload

    final_report = payload.get('final_report')
    final_report_dict = dict(final_report) if isinstance(final_report, dict) else {}

    fix_cards: list[dict] = []
    if isinstance(final_report_dict.get('findings'), list):
        fix_cards = [item for item in final_report_dict['findings'] if isinstance(item, dict)]

    if not fix_cards:
        prior_reports = final_report_dict.get('prior_reports')
        if isinstance(prior_reports, list):
            for pr in prior_reports:
                if isinstance(pr, dict) and isinstance(pr.get('findings'), list):
                    for item in pr['findings']:
                        if isinstance(item, dict):
                            fix_cards.append(item)

    if not fix_cards:
        return payload

    payload['fixes'] = {'fixes': fix_cards}

    if not isinstance(final_report_dict.get('findings'), list) or not final_report_dict.get('findings'):
        final_report_dict['findings'] = fix_cards
        metrics = final_report_dict.get('metrics') if isinstance(final_report_dict.get('metrics'), dict) else {}
        summary = final_report_dict.get('summary') if isinstance(final_report_dict.get('summary'), dict) else {}
        metrics = dict(metrics)
        summary = dict(summary)
        metrics['fixes_generated'] = len(fix_cards)
        summary['items_flagged'] = len(fix_cards)
        summary['items_passed'] = max(int(summary.get('total_items_analyzed') or len(fix_cards)) - len(fix_cards), 0)
        final_report_dict['metrics'] = metrics
        final_report_dict['summary'] = summary
        payload['final_report'] = final_report_dict

    try:
        save_analysis_result(
            job_id=job_id,
            result=payload,
            analysis_type=analysis_type,
        )
    except Exception:
        # Never fail response path because of backfill persistence.
        pass

    return payload


def _enrich_report_download_meta(job_id: str, result: dict) -> dict:
    payload = dict(result or {})
    report_path = payload.get('report_file_path')
    has_report_file = bool(report_path and os.path.exists(report_path))
    export_payload = _build_postgres_export_payload(payload)
    has_embedded_report = bool(export_payload.get('reports'))
    has_report = has_report_file or has_embedded_report

    payload['report_download_available'] = has_report
    payload['report_download_url'] = f'/api/results/{job_id}/export' if has_report else None
    payload['report_meta'] = {
        'database_id': payload.get('report_database_id') or payload.get('database_id'),
        'run_timestamp': payload.get('report_timestamp') or payload.get('final_report', {}).get('run_timestamp'),
        'file_name': payload.get('report_file_name') or (f'analysis-report-{job_id}.json' if has_report else None),
    }
    # Keep response payload lean; full JSON bundle is available through /export.
    payload.pop('download_report_json', None)
    return payload


def _extract_agent_reports_for_export(result: dict) -> dict:
    payload = dict(result or {})

    source_reports = payload.get('agent_reports')
    if isinstance(source_reports, dict) and source_reports:
        export_reports = {
            key: value
            for key, value in source_reports.items()
            if isinstance(value, dict)
        }
        if export_reports:
            return export_reports

    export_reports = {}

    schema_json = (payload.get('schema_analysis') or {}).get('json_report')
    if isinstance(schema_json, dict):
        export_reports['schema_integrity_validator'] = schema_json

    column_json = payload.get('column_validator_analysis')
    if isinstance(column_json, dict):
        export_reports['column_validator'] = {
            key: value
            for key, value in column_json.items()
            if key not in {'report', 'markdown_report'}
        }

    row_json = (payload.get('row_level_analysis') or {}).get('json_report')
    if isinstance(row_json, dict):
        export_reports['row_level_validator'] = row_json

    cross_json = (payload.get('cross_row_analysis') or {}).get('json_report')
    if isinstance(cross_json, dict):
        export_reports['cross_row_validator'] = cross_json

    final_report = payload.get('final_report')
    if isinstance(final_report, dict):
        export_reports['data_fix_planner'] = final_report

    return export_reports


def _build_postgres_export_payload(result: dict) -> dict:
    payload = dict(result or {})

    inline_bundle = payload.get('download_report_json')
    if isinstance(inline_bundle, dict) and inline_bundle.get('reports'):
        return inline_bundle

    reports = _extract_agent_reports_for_export(payload)

    return {
        'pipeline_run_id': payload.get('pipeline_run_id') or payload.get('final_report', {}).get('pipeline_run_id'),
        'database_id': payload.get('database_id') or payload.get('report_database_id') or payload.get('final_report', {}).get('database_id'),
        'run_timestamp': payload.get('report_timestamp') or payload.get('final_report', {}).get('run_timestamp'),
        'summary': {
            'confidence_score': payload.get('confidence_score'),
            'data_quality_score': payload.get('data_quality_score'),
        },
        'reports': reports,
    }


def _extract_quality_score(result_payload: dict | None) -> float | None:
    payload = result_payload or {}

    candidates = [
        payload.get('data_quality_score'),
        ((payload.get('column_validator_analysis') or {}).get('summary') or {}).get('overall_quality_score'),
        (payload.get('quality_report') or {}).get('quality_score'),
        ((payload.get('final_report') or {}).get('summary') or {}).get('data_quality_score'),
    ]
    for raw in candidates:
        try:
            value = float(raw)
        except (TypeError, ValueError):
            continue
        if math.isnan(value) or math.isinf(value):
            continue
        return round(value, 4)

    return None


def _normalize_flagged_tables(values: list[Any]) -> list[str]:
    normalized: list[str] = []
    seen: set[str] = set()
    for raw in values:
        table = str(raw or '').strip()
        if not table:
            continue
        key = table.lower()
        if key in seen:
            continue
        seen.add(key)
        normalized.append(table)
    return sorted(normalized, key=str.lower)


def _extract_flagged_tables(result_payload: dict | None) -> list[str]:
    payload = result_payload or {}

    column_summary = (payload.get('column_validator_analysis') or {}).get('summary') or {}
    flagged = column_summary.get('flagged_tables')
    if isinstance(flagged, list):
        return _normalize_flagged_tables(flagged)

    findings = (payload.get('final_report') or {}).get('findings')
    if not isinstance(findings, list):
        return []

    inferred: list[Any] = []
    for item in findings:
        if not isinstance(item, dict):
            continue
        table = item.get('table') or item.get('table_name')
        if table:
            inferred.append(table)

    return _normalize_flagged_tables(inferred)


def _extract_pipeline_run_id(result_payload: dict | None) -> str | None:
    payload = result_payload or {}
    run_id = (payload.get('pipeline_run_id') or '').strip()
    if run_id:
        return run_id

    final_report = payload.get('final_report')
    if isinstance(final_report, dict):
        nested = (final_report.get('pipeline_run_id') or '').strip()
        if nested:
            return nested

    return None


_CHAT_MARKDOWN_STYLE_DIRECTIVE = (
    "[RESPONSE STYLE]\n"
    "Respond in GitHub-flavored Markdown only.\n"
    "Tone: professional, friendly, and practical.\n"
    "For problem discussions, include short sections for Problem, Impact, and Recommendation.\n"
    "Use headings and bullet points when useful.\n"
    "Do not output raw JSON unless explicitly requested by the user."
)

_MARKDOWN_BLOCK_RE = re.compile(r"(?m)^\s*(#{1,6}\s|[-*+]\s|\d+\.\s|>\s|```)")


def _looks_like_markdown(text: str) -> bool:
    candidate = text or ""
    if not candidate:
        return False
    if _MARKDOWN_BLOCK_RE.search(candidate):
        return True
    return "**" in candidate or "`" in candidate


def _build_agent_markdown_prompt(*, message: str, analysis_context: str = "") -> str:
    blocks = [_CHAT_MARKDOWN_STYLE_DIRECTIVE]

    if analysis_context:
        blocks.append(
            "[CONTEXT: The following analysis was already completed for this database. "
            "Use it to answer the user's question. Do NOT ask for a connection string again.]\n\n"
            f"{analysis_context}"
        )

    blocks.append(f"[USER QUESTION]\n{message}")
    return "\n\n---\n\n".join(blocks)


def _normalize_agent_markdown_response(raw_content: Any) -> str:
    text = str(raw_content or "").replace("\r\n", "\n").strip()
    if not text:
        return (
            "## Response Update\n\n"
            "I could not generate a detailed answer in this attempt.\n\n"
            "- **Problem:** The agent response was empty.\n"
            "- **Impact:** No actionable guidance was returned.\n"
            "- **Recommendation:** Please retry with a more specific question."
        )

    if _looks_like_markdown(text):
        return text

    lines = [line.strip() for line in text.split("\n") if line.strip()]
    if not lines:
        return (
            "## Response Update\n\n"
            "I could not generate a detailed answer in this attempt.\n\n"
            "- **Problem:** The agent response was empty.\n"
            "- **Impact:** No actionable guidance was returned.\n"
            "- **Recommendation:** Please retry with a more specific question."
        )

    if len(lines) == 1:
        return (
            "## Response Update\n\n"
            f"{lines[0]}\n\n"
            "- **Problem:** The source response was plain text.\n"
            "- **Impact:** Structure may be harder to scan quickly.\n"
            "- **Recommendation:** Ask a focused follow-up if you want a step-by-step breakdown."
        )

    key_points = "\n".join(f"- {line}" for line in lines)
    return (
        "## Response Update\n\n"
        "### Key Points\n"
        f"{key_points}\n\n"
        "- **Problem:** The source response was plain text.\n"
        "- **Impact:** Formatting consistency can be reduced across the conversation.\n"
        "- **Recommendation:** The response has been normalized to Markdown for readability."
    )


class TestPostgresConnectionView(APIView):
    """
    POST /api/test-postgres — test connectivity to a PostgreSQL database.
    Body: { "connection_string": "..." }
    Returns: { "success": true, "database": "...", "host": "..." }
    """
    permission_classes = [IsAuthenticated]

    def post(self, request):
        conn_str = request.data.get('connection_string', '').strip()
        if not conn_str:
            return Response({'error': 'connection_string is required'}, status=status.HTTP_400_BAD_REQUEST)

        if not looks_like_postgres_dsn(conn_str):
            return Response(
                {'error': 'Invalid connection string format. Expected: postgresql://user:password@host:port/database'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            test_postgres_connection(conn_str, timeout_s=3)
        except Exception as exc:
            return Response(
                {
                    'error': 'Could not connect to PostgreSQL.',
                    'detail': str(exc),
                    'target': redact_postgres_dsn(conn_str),
                },
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )

        from services.postgres_connection import parse_postgres_dsn_info
        info = parse_postgres_dsn_info(conn_str)
        return Response({
            'success': True,
            'database': info.database,
            'host': info.host,
            'port': info.port,
        })

class AnalyzePostgresView(APIView):
    """
    POST /api/analyze-postgres — start agentic analysis on a PostgreSQL database.
    Body: { "connection_string": "...", "domain_rules": [...], "llm_model": "..." }
    """
    permission_classes = [IsAuthenticated]
    throttle_classes = [AnalyzePostgresUserRateThrottle]

    def post(self, request):
        unverified = _require_verified_email(request)
        if unverified:
            return unverified

        serializer = AnalyzePostgresSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

        conn_str = serializer.validated_data.get('connection_string')
        domain_rules = serializer.validated_data.get('domain_rules', [])
        llm_model = serializer.validated_data.get('llm_model', 'gemini-2.0-flash').strip() or 'gemini-2.0-flash'
        target_sandbox = serializer.validated_data.get('target_sandbox', False)
        baseline_job_id = serializer.validated_data.get('baseline_job_id')
        
        baseline_job = get_job(baseline_job_id) if baseline_job_id else None
        if baseline_job:
            if not conn_str and baseline_job.get('connection_string'):
                conn_str = baseline_job.get('connection_string')
            # Re-runs always use the original job's model, not the request default.
            original_model = (baseline_job.get('llm_model') or '').strip()
            if original_model:
                llm_model = original_model

        # If targeting sandbox, append _clean to the database name
        if target_sandbox and conn_str:
            try:
                from urllib.parse import urlparse, urlunparse
                p = urlparse(conn_str)
                if p.path and not p.path.endswith('_clean'):
                    new_path = p.path + "_clean"
                    p = p._replace(path=new_path)
                    conn_str = urlunparse(p)
            except Exception:
                pass

        # 1) Quick format check (pattern) to fail fast on obvious mistakes.
        if not looks_like_postgres_dsn(conn_str):
            return Response(
                {
                    'error': (
                        "Invalid connection_string format. Expected: "
                        "postgresql://user:password@host:port/database"
                    )
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        # 2) Preflight connectivity check so we don’t enqueue a job that can’t run.
        try:
            test_postgres_connection(conn_str, timeout_s=3)
        except RuntimeError as exc:
            return Response(
                {
                    'error': 'Could not connect to PostgreSQL (check host/port/credentials).',
                    'detail': str(exc),
                    'target': redact_postgres_dsn(conn_str),
                },
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )
        except Exception as exc:
            # ValueError => bad DSN; RuntimeError => connectivity; keep it safe.
            return Response(
                {
                    'error': 'PostgreSQL connection check failed.',
                    'detail': str(exc),
                    'target': redact_postgres_dsn(conn_str),
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        from services.postgres_connection import parse_postgres_dsn_info
        try:
            dsn_info = parse_postgres_dsn_info(conn_str)
            db_label = dsn_info.database or 'PostgreSQL Connection'
        except Exception:
            db_label = 'PostgreSQL Connection'

        user_id = str(request.user.id)

        # Create a job tracking entry
        job_id = create_job(
            user_id=user_id,
            mode="agentic_postgres",
            llm_model=llm_model,
        )

        # Persist the DSN in the job document for task retrieval.
        update_job_connection_string(job_id, conn_str)

        async_result = analyze_postgres_task.delay(
            job_id=job_id,
            connection_string=conn_str,
            domain_rules=domain_rules,
            llm_model=llm_model,
            baseline_job_id=baseline_job_id,
        )
        task_id = str(getattr(async_result, 'id', '') or '')
        if task_id:
            update_job_celery_task_id(job_id, task_id)

        audit_log(job_id, 'DB_ANALYSIS_ENQUEUED', {'task_id': task_id}, user_id=user_id)
        return Response({'job_id': job_id, 'message': 'Postgres analysis started', 'task_id': task_id})


class StatusView(APIView):
    """
    GET /api/status/<jobId> — get job status.
    """
    permission_classes = [IsAuthenticated]

    def get(self, request, jobId):
        job = find_owned_job(jobId, request.user)
        job_status = job['status'] if job else 'unknown'
        response_payload = {'status': job_status}

        if job_status == 'completed':
            analysis = get_analysis_result(jobId)
            result = (analysis or {}).get('result', {})
            if result.get('schema_analysis'):
                result = _ensure_column_validator_result(jobId, analysis)
                result = _enrich_report_download_meta(jobId, result)

            report_path = result.get('report_file_path')
            has_download = bool(result.get('report_download_available'))
            if has_download or (report_path and os.path.exists(report_path)):
                response_payload['url'] = f'/api/results/{jobId}/export'

        return Response(response_payload)


class ResultsView(APIView):
    """
    GET /api/results/<jobId> — get analysis results.

    Returns { cleaned_data, quality_report } matching the Node.js response shape.
    """
    permission_classes = [IsAuthenticated]

    def get(self, request, jobId):
        job = find_owned_job(jobId, request.user)
        if not job:
            return Response({'detail': 'Results not found'}, status=status.HTTP_404_NOT_FOUND)

        analysis = get_analysis_result(jobId)
        if not analysis or not analysis.get('result'):
            return Response({'detail': 'Results not found'}, status=status.HTTP_404_NOT_FOUND)

        result = analysis['result']
        if 'schema_analysis' in result:
            result = _ensure_column_validator_result(jobId, analysis)
            result = _ensure_fix_cards_result(jobId, result, analysis.get('analysis_type', 'postgres_agentic'))
            result = _enrich_report_download_meta(jobId, result)
            
            # Check if any fix was successfully executed for this job
            has_executed_fixes = AuditLog.objects.filter(
                job_id=jobId,
                event_type='FIX_EXECUTED',
                payload__status_code=200
            ).exists()
            result['has_executed_fixes'] = has_executed_fixes

        return Response(_sanitize_for_json(result))


class ResultsDiffView(APIView):
    """
    GET /api/results/<jobId>/diff — compare current run against previous run.

    Query params:
      - compare_to: optional job id override for the baseline run
    """
    permission_classes = [IsAuthenticated]

    def get(self, request, jobId):
        job = find_owned_job(jobId, request.user)
        if not job:
            return Response({'detail': 'Results not found'}, status=status.HTTP_404_NOT_FOUND)

        current = (
            AnalysisResult.objects
            .select_related('job', 'previous_run', 'previous_run__job')
            .filter(job_id=jobId)
            .first()
        )
        if not current:
            return Response({'detail': 'Results not found'}, status=status.HTTP_404_NOT_FOUND)

        compare_to = (request.query_params.get('compare_to') or '').strip()
        if compare_to:
            baseline_job = find_owned_job(compare_to, request.user)
            if not baseline_job:
                return Response({'detail': 'Comparison run not found'}, status=status.HTTP_404_NOT_FOUND)
            baseline = (
                AnalysisResult.objects
                .select_related('job')
                .filter(job_id=compare_to)
                .first()
            )
            if not baseline:
                return Response({'detail': 'Comparison run has no analysis result'}, status=status.HTTP_404_NOT_FOUND)
        else:
            baseline = current.previous_run
            if baseline is None:
                return Response({'detail': 'No previous run found for this job'}, status=status.HTTP_404_NOT_FOUND)
            if request.user.role != 'admin' and str(baseline.job.user_id) != str(request.user.id):
                return Response({'detail': 'Comparison run not found'}, status=status.HTTP_404_NOT_FOUND)

        current_result = dict(current.result or {})
        baseline_result = dict(baseline.result or {})

        current_quality = _extract_quality_score(current_result)
        baseline_quality = _extract_quality_score(baseline_result)
        quality_delta = (
            round(current_quality - baseline_quality, 4)
            if current_quality is not None and baseline_quality is not None
            else None
        )

        if quality_delta is None:
            trend = 'unknown'
        elif quality_delta > 0:
            trend = 'improved'
        elif quality_delta < 0:
            trend = 'degraded'
        else:
            trend = 'unchanged'

        current_tables = _extract_flagged_tables(current_result)
        baseline_tables = _extract_flagged_tables(baseline_result)
        current_by_key = {value.lower(): value for value in current_tables}
        baseline_by_key = {value.lower(): value for value in baseline_tables}

        new_keys = set(current_by_key.keys()) - set(baseline_by_key.keys())
        resolved_keys = set(baseline_by_key.keys()) - set(current_by_key.keys())
        unchanged_keys = set(current_by_key.keys()) & set(baseline_by_key.keys())

        return Response(
            {
                'current_run': {
                    'job_id': str(current.job_id),
                    'pipeline_run_id': _extract_pipeline_run_id(current_result),
                    'database_id': current_result.get('database_id') or current_result.get('report_database_id'),
                    'created_at': current.created_at.isoformat() if current.created_at else None,
                    'quality_score': current_quality,
                    'flagged_tables': current_tables,
                    'flagged_table_count': len(current_tables),
                },
                'previous_run': {
                    'job_id': str(baseline.job_id),
                    'pipeline_run_id': _extract_pipeline_run_id(baseline_result),
                    'database_id': baseline_result.get('database_id') or baseline_result.get('report_database_id'),
                    'created_at': baseline.created_at.isoformat() if baseline.created_at else None,
                    'quality_score': baseline_quality,
                    'flagged_tables': baseline_tables,
                    'flagged_table_count': len(baseline_tables),
                },
                'diff': {
                    'quality_score': {
                        'delta': quality_delta,
                        'trend': trend,
                    },
                    'flagged_tables': {
                        'new': sorted([current_by_key[key] for key in new_keys], key=str.lower),
                        'resolved': sorted([baseline_by_key[key] for key in resolved_keys], key=str.lower),
                        'unchanged': sorted([current_by_key[key] for key in unchanged_keys], key=str.lower),
                    },
                },
            }
        )


class ResultsExportView(APIView):
    """
    GET /api/results/<jobId>/export — download full analysis report.

    Returns structured report matching Node.js export format.
    """
    permission_classes = [IsAuthenticated]

    def get(self, request, jobId):
        job = find_owned_job(jobId, request.user)
        if not job:
            return Response({'detail': 'Results not found'}, status=status.HTTP_404_NOT_FOUND)

        analysis = get_analysis_result(jobId)
        if not analysis or not analysis.get('result'):
            return Response({'detail': 'Results not found'}, status=status.HTTP_404_NOT_FOUND)

        data = analysis['result']
        if 'schema_analysis' in data:
            data = _ensure_column_validator_result(jobId, analysis)

            report_path = data.get('report_file_path')
            if report_path and os.path.exists(report_path):
                filename = data.get('report_file_name') or f'analysis-report-{jobId}.json'
                return FileResponse(
                    open(report_path, 'rb'),
                    content_type='application/json',
                    as_attachment=True,
                    filename=filename,
                )

            export_payload = _build_postgres_export_payload(data)
            if export_payload.get('reports'):
                return Response(_sanitize_for_json(export_payload))

        # If already in agentic PostgreSQL format, return as-is (sanitized)
        # so all generated keys are preserved.
        if 'dataset_summary' in data or 'schema_analysis' in data:
            return Response(_sanitize_for_json(data))

        meta = data.get('meta', {})
        qr = _sanitize_for_json(data.get('quality_report', {}))
        cleaned = data.get('cleaned_data', {})

        export_report = {
            'meta': {
                'job_id': jobId,
                'file_path': meta.get('file_path'),
                'file_size_bytes': meta.get('file_size_bytes'),
                'mode': meta.get('mode', 'normal'),
                'analysis_type': meta.get('analysis_type', 'in-memory'),
                'started_at': meta.get('started_at'),
                'completed_at': meta.get('completed_at'),
                'duration_ms': meta.get('duration_ms'),
                'version': '1.0',
            },
            'dataset_summary': {
                'rows': cleaned.get('rows', 0),
                'columns': cleaned.get('columns', 0),
            },
            'quality_overview': {
                'missing_rate': qr.get('missing_rate'),
                'duplicate_rate': qr.get('duplicate_rate'),
                'error_rate': qr.get('outlier_rate'),
            },
            'column_analysis': qr.get('column_analysis', {}),
            'limitations': {
                'approximate_metrics': False,
                'skipped_checks': [],
            },
        }

        return Response(export_report)


# ──────────────────────────────────────────────
# Jobs (History)
# ──────────────────────────────────────────────

class JobsView(APIView):
    """
    GET    /api/jobs — list all jobs for the authenticated user.
    DELETE /api/jobs — bulk delete jobs by ID.

    Combined into one view since they share the same URL path (matches Node.js).
    """
    permission_classes = [IsAuthenticated]

    def get(self, request):
        """List jobs. Admins see all; regular users see only their own."""
        user_id = str(request.user.id)
        is_admin = request.user.role == 'admin'

        jobs = get_jobs_for_user(user_id, is_admin=is_admin)
        job_ids = [j['id'] for j in jobs]

        user_map = {}
        if is_admin:
            user_ids = {j.get('user_id') for j in jobs if j.get('user_id')}
            uuid_ids = []
            for uid in user_ids:
                try:
                    uuid_ids.append(uuid.UUID(uid))
                except ValueError:
                    continue
            users = User.objects.filter(id__in=uuid_ids).only('id', 'email', 'username')
            user_map = {str(u.id): u for u in users}

        # Batch-check which jobs have analysis/chat results
        analysis_set = get_analysis_job_ids(job_ids) if job_ids else set()
        chat_set = get_chat_job_ids(job_ids) if job_ids else set()

        # Build a connection_string → db_name lookup for every job
        job_ids_list = [j['id'] for j in jobs]
        conn_str_map: dict[str, str] = {}
        if job_ids_list:
            from api.models import Job as JobModel
            from api.connection_string_crypto import decrypt_connection_string
            from services.postgres_connection import parse_postgres_dsn_info
            qs = JobModel.objects.filter(
                id__in=job_ids_list,
            ).only('id', 'connection_string')
            for jobj in qs:
                encrypted_cs = jobj.connection_string or ''
                if encrypted_cs:
                    try:
                        plain_cs = decrypt_connection_string(encrypted_cs) or ''
                        db_label = parse_postgres_dsn_info(plain_cs).database or '' if plain_cs else ''
                    except Exception:
                        db_label = ''
                    if db_label:
                        conn_str_map[str(jobj.id)] = db_label

        result = [
            {
                '_id': j['id'],
                'db_name': conn_str_map.get(j['id'], ''),
                'status': j.get('status'),
                'mode': j.get('mode'),
                'created_at': j.get('created_at'),
                'has_analysis': j['id'] in analysis_set,
                'has_chat': j['id'] in chat_set,
                **(
                    {
                        'owner_email': (
                            user_map.get(j.get('user_id')).email
                            if user_map.get(j.get('user_id'))
                            else None
                        ),
                        'owner_username': (
                            user_map.get(j.get('user_id')).username
                            if user_map.get(j.get('user_id'))
                            else None
                        ),
                    }
                    if is_admin
                    else {}
                ),
            }
            for j in jobs
        ]

        return Response(result)

    def delete(self, request):
        """Bulk delete jobs. Body: { job_ids: ["id1", "id2", ...] }"""
        unverified = _require_verified_email(request)
        if unverified:
            return unverified

        serializer = JobDeleteSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(
                {'error': 'job_ids array is required'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        job_ids = serializer.validated_data['job_ids']
        user_id = str(request.user.id)
        is_admin = request.user.role == 'admin'

        # Delete from PostgreSQL (with ownership enforcement)
        deleted_jobs = delete_jobs(job_ids, user_id=user_id, is_admin=is_admin)

        if not deleted_jobs:
            return Response(
                {'error': 'No matching jobs found'},
                status=status.HTTP_404_NOT_FOUND,
            )

        # Delete files on disk (best-effort)
        for job in deleted_jobs:
            file_path = job.get('file_path')
            cleaned_path = job.get('cleaned_file_path')
            if file_path:
                try:
                    os.unlink(file_path)
                except OSError:
                    pass
            if cleaned_path:
                try:
                    os.unlink(cleaned_path)
                except OSError:
                    pass

        # Audit log each deletion
        owned_ids = [j['id'] for j in deleted_jobs]
        for jid in owned_ids:
            audit_log(jid, 'JOB_DELETED', {'deleted_by': user_id}, user_id=user_id)

        return Response({'deleted': len(owned_ids), 'job_ids': owned_ids})


class JobDetailView(APIView):
    """
    DELETE /api/jobs/<job_id>/ — cancel a running job.
    """
    permission_classes = [IsAuthenticated]

    def delete(self, request, job_id):
        job = find_owned_job(job_id, request.user)
        if not job:
            return Response({'detail': 'Job not found'}, status=status.HTTP_404_NOT_FOUND)

        current_status = str(job.get('status') or '').strip()
        if current_status.upper() == 'CANCELLED':
            return Response({'job_id': job_id, 'status': 'CANCELLED', 'already_cancelled': True})

        if not _is_active_job_status(current_status):
            return Response(
                {
                    'error': 'Only running jobs can be cancelled.',
                    'status': current_status,
                },
                status=status.HTTP_409_CONFLICT,
            )

        task_id = str(job.get('celery_task_id') or '').strip()
        revoked = False
        revoke_error = None

        if task_id:
            try:
                celery_app.control.revoke(task_id, terminate=True)
                revoked = True
            except Exception as exc:
                logger.exception('Failed to revoke Celery task %s for job %s', task_id, job_id)
                revoke_error = str(exc)

        update_job_status(job_id, 'CANCELLED')
        update_job_celery_task_id(job_id, None)

        audit_payload = {
            'cancelled_by': str(request.user.id),
            'previous_status': current_status,
            'task_id': task_id or None,
            'revoke_requested': bool(task_id),
            'revoked': revoked,
        }
        if revoke_error:
            audit_payload['revoke_error'] = revoke_error
        audit_log(job_id, 'JOB_CANCELLED', audit_payload, user_id=str(request.user.id))

        response_payload = {
            'job_id': job_id,
            'status': 'CANCELLED',
            'task_id': task_id or None,
            'revoked': revoked,
        }
        if revoke_error:
            response_payload['revoke_error'] = revoke_error

        # Notify the job owner that the job was cancelled
        try:
            cancelled_job_user = User.objects.filter(
                id=job.get('user_id')
            ).only('id', 'email', 'email_notifications_enabled').first()
            if cancelled_job_user and bool(getattr(cancelled_job_user, 'email_notifications_enabled', True)):
                user_email = getattr(cancelled_job_user, 'email', '')
                if user_email:
                    # Extract db_name from the decrypted connection string
                    _cancel_db_name = None
                    _cs = job.get('connection_string')
                    if _cs:
                        try:
                            from services.postgres_connection import parse_postgres_dsn_info
                            _cancel_db_name = parse_postgres_dsn_info(_cs).database
                        except Exception:
                            pass
                    # Format analysis_date from created_at
                    _cancel_date = None
                    _raw_ts = job.get('created_at')
                    if _raw_ts:
                        try:
                            if hasattr(_raw_ts, 'strftime'):
                                _cancel_date = _raw_ts.strftime('%Y-%m-%d %H:%M UTC')
                            else:
                                from datetime import datetime as _dt
                                _cancel_date = _dt.fromisoformat(
                                    str(_raw_ts).replace('Z', '+00:00')
                                ).strftime('%Y-%m-%d %H:%M UTC')
                        except Exception:
                            _cancel_date = str(_raw_ts)
                    notify_user_job_cancelled(user_email, job_id, _cancel_db_name, _cancel_date)
        except Exception:
            logger.exception('Failed to send cancellation notification for job %s', job_id)

        return Response(response_payload)


# ──────────────────────────────────────────────
# AgentOS Chat Proxy
# ──────────────────────────────────────────────

AGENT_OS_BASE_URL = os.environ.get('AGENT_OS_URL', 'http://localhost:7777')
# Must match Team(id=...) in agents/team.py — agent_os.py registers the team
# under that id and AgentOS routes /teams/<id>/runs from it.
AGENT_OS_TEAM_ID  = os.environ.get('AGENT_OS_TEAM_ID', 'qualitylab-team')
_DEFAULT_CHAT_LLM_MODEL = 'gemini-2.0-flash'


class AgentChatProxyView(APIView):
    """
    POST /api/agent-chat

    Proxies a chat message from the Angular frontend to the AgentOS team
    running on port 7777.  Keeps CORS clean and adds JWT auth protection.

    Body: { "message": str, "session_id": str, "job_id"?: str, "llm_model"?: str }
    Returns: { "content": str }

    When job_id is provided, the analysis results are fetched from PostgreSQL
    and injected as context so the agent can answer follow-up questions
    about the completed analysis.
    """
    permission_classes = [IsAuthenticated]

    def post(self, request):
        message    = request.data.get('message', '').strip()
        session_id = request.data.get('session_id', '')
        job_id     = request.data.get('job_id', '')
        llm_model  = (request.data.get('llm_model') or '').strip()
        resolved_llm_model = llm_model or _DEFAULT_CHAT_LLM_MODEL

        if not message:
            return Response({'error': 'message is required'}, status=status.HTTP_400_BAD_REQUEST)

        if job_id and not llm_model:
            try:
                job = get_job(job_id) or {}
                job_llm_model = (job.get('llm_model') or '').strip()
                if job_llm_model:
                    resolved_llm_model = job_llm_model
            except Exception:
                pass

        # Build the message with analysis context if a job_id is provided
        analysis_context = ''
        if job_id:
            try:
                analysis = get_analysis_result(job_id)
                if analysis and 'result' in analysis:
                    result = analysis['result']
                    context_parts = []
                    if result.get('schema_analysis', {}).get('report'):
                        context_parts.append(f"## Schema Analysis Report\n{result['schema_analysis']['report']}")
                    if result.get('statistical_analysis', {}).get('report'):
                        context_parts.append(f"## Statistical Validator Report\n{result['statistical_analysis']['report']}")
                    if result.get('structural_analysis', {}).get('report'):
                        context_parts.append(f"## Structural Validator Report\n{result['structural_analysis']['report']}")
                    if result.get('relational_analysis', {}).get('report'):
                        context_parts.append(f"## Relational Validator Report\n{result['relational_analysis']['report']}")
                    if context_parts:
                        analysis_context = '\n\n---\n\n'.join(context_parts)
            except Exception:
                pass  # Fall back to raw message if context fetch fails

        enriched_message = _build_agent_markdown_prompt(
            message=message,
            analysis_context=analysis_context,
        )

        url = f'{AGENT_OS_BASE_URL}/teams/{AGENT_OS_TEAM_ID}/runs'

        try:
            resp = http_requests.post(
                url,
                data={
                    'message':    enriched_message,
                    'session_id': session_id,
                    'user_id':    str(request.user.id),
                    'stream':     'False',
                    'llm_model':  resolved_llm_model,
                },
                timeout=120,   # agents can be slow
            )
            resp.raise_for_status()

            data = resp.json()
            raw_content = data.get('content') or data.get('output') or str(data)
            content = _normalize_agent_markdown_response(raw_content)

            # Persist both messages to PostgreSQL
            if job_id:
                user_id = str(request.user.id)
                save_chat_message(job_id, user_id, 'user', message)
                save_chat_message(job_id, user_id, 'agent', content)

            return Response({'content': content, 'format': 'markdown'})

        except http_requests.exceptions.ConnectionError:
            return Response(
                {
                    'error': (
                        "## AgentOS Unavailable\n\n"
                        "- **Problem:** The AgentOS server is unreachable.\n"
                        "- **Impact:** Follow-up assistant responses cannot be generated right now.\n"
                        "- **Recommendation:** Start the AgentOS server with python agents/agent_os.py and retry."
                    )
                },
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )
        except http_requests.exceptions.Timeout:
            return Response(
                {
                    'error': (
                        "## Agent Timeout\n\n"
                        "- **Problem:** The agent request exceeded the response timeout.\n"
                        "- **Impact:** No complete answer was produced for this question.\n"
                        "- **Recommendation:** Narrow the question scope and retry."
                    )
                },
                status=status.HTTP_504_GATEWAY_TIMEOUT,
            )
        except Exception as exc:
            return Response(
                {
                    'error': (
                        "## Agent Request Failed\n\n"
                        f"- **Problem:** {str(exc).strip() or 'Unknown proxy error.'}\n"
                        "- **Impact:** The assistant could not return a follow-up response.\n"
                        "- **Recommendation:** Retry the request. If it persists, review AgentOS server logs."
                    )
                },
                status=status.HTTP_502_BAD_GATEWAY,
            )


class JobChatView(APIView):
    """
    GET/POST /api/jobs/<job_id>/chat/ — list or append chat messages for a job.
    """
    permission_classes = [IsAuthenticated]

    def get(self, request, job_id):
        job = find_owned_job(job_id, request.user)
        if not job:
            return Response({'error': 'Job not found'}, status=status.HTTP_404_NOT_FOUND)

        messages = get_chat_messages(job_id)
        for message in messages:
            if 'created_at' in message:
                message['created_at'] = message['created_at'].isoformat()

        return Response({'messages': messages})

    def post(self, request, job_id):
        job = find_owned_job(job_id, request.user)
        if not job:
            return Response({'error': 'Job not found'}, status=status.HTTP_404_NOT_FOUND)

        serializer = ChatMessageCreateSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

        role = serializer.validated_data['role']
        content = serializer.validated_data['content']
        if role == 'agent':
            content = _normalize_agent_markdown_response(content)

        save_chat_message(
            job_id,
            str(request.user.id),
            role,
            content,
        )
        return Response({'ok': True})


class ChatHistoryView(JobChatView):
    """
    Backward-compatible alias for GET/POST /api/chat-history/<jobId>.
    """

    def get(self, request, jobId):
        return super().get(request, jobId)

    def post(self, request, jobId):
        return super().post(request, jobId)


# ──────────────────────────────────────────────
# PostgreSQL Human-in-the-Loop Fix Execution
# ──────────────────────────────────────────────

class GetFixesView(APIView):
    """
    GET /api/fixes/<job_id>/

    Returns the list of AI-generated fix cards stored in
    analysis_results.result.fixes for this job.
    """
    permission_classes = [IsAuthenticated]

    def get(self, request, job_id):
        job = find_owned_job(job_id, request.user)
        if not job:
            return Response(
                {'detail': 'Job not found or access denied.'},
                status=status.HTTP_404_NOT_FOUND,
            )

        analysis = get_analysis_result(job_id)
        result = (analysis or {}).get('result', {})
        if 'schema_analysis' in result:
            result = _ensure_fix_cards_result(
                job_id,
                result,
                (analysis or {}).get('analysis_type', 'postgres_agentic'),
            )

        fixes = (result or {}).get('fixes', None)
        if fixes is None:
            return Response(
                {'detail': 'No fixes found. Re-run analysis to generate fix cards.'},
                status=status.HTTP_404_NOT_FOUND,
            )

        # Normalize to a dict so we can attach database_name for the UI.
        if isinstance(fixes, list):
            fixes_payload = {'fixes': fixes}
        elif isinstance(fixes, dict):
            fixes_payload = dict(fixes)
        else:
            fixes_payload = {'fixes': []}

        conn_str = job.get('connection_string') or ''
        if conn_str:
            try:
                from urllib.parse import urlparse as _up
                fixes_payload['database_name'] = (_up(conn_str).path or '').lstrip('/') or ''
            except Exception:
                pass

        return Response(fixes_payload)


def _record_learning_pattern(user, rule_family: str, entity: str, decision: str) -> None:
    """Upsert a LearningPattern row for the given user+rule_family+entity.

    Uses get_or_create + F() increment so concurrent requests don't race.
    """
    from django.db.models import F
    from .models import LearningPattern

    if not rule_family or not entity or decision not in (
        LearningPattern.DECISION_ACCEPT, LearningPattern.DECISION_REJECT
    ):
        return

    obj, created = LearningPattern.objects.get_or_create(
        user=user,
        rule_family=rule_family,
        entity=entity,
        defaults={'decision': decision, 'frequency': 1},
    )
    if not created:
        # Update decision and increment frequency atomically.
        LearningPattern.objects.filter(pk=obj.pk).update(
            decision=decision,
            frequency=F('frequency') + 1,
        )


class ExecuteFixView(APIView):
    """
    POST /api/execute-fix/<job_id>/

    Body: { "queries": ["SQL1", "SQL2", ...], "rule_family": "...", "entity": "...", "decision": "accept"|"reject" }

        Executes the supplied SQL statements for a single fix card against the
        user's PostgreSQL database inside one transaction.

    Returns:
            strict execution log payload with status, transaction outcome,
            duration, and per-query details.
    """
    permission_classes = [IsAuthenticated]
    throttle_classes = [ExecuteFixUserRateThrottle]

    def post(self, request, job_id):
        unverified = _require_verified_email(request)
        if unverified:
            return unverified

        # 1 — Ownership
        job = find_owned_job(job_id, request.user)
        if not job:
            return Response(
                {'detail': 'Job not found or access denied.'},
                status=status.HTTP_404_NOT_FOUND,
            )

        # 2 — Connection string
        connection_string = job.get('connection_string')
        if not connection_string:
            return Response(
                {'detail': 'No connection string on this job.'},
                status=status.HTTP_422_UNPROCESSABLE_ENTITY,
            )

        # 3 — Validate input
        queries = request.data.get('queries')
        rule_family = str(request.data.get('rule_family') or '').strip()
        entity      = str(request.data.get('entity')      or '').strip()
        decision    = str(request.data.get('decision')    or '').strip().lower()
        fix_id = str(request.data.get('fix_id') or '').strip()
        interaction_model = str(request.data.get('interaction_model') or '').strip()
        source_rule = str(request.data.get('source_rule') or '').strip()
        source_type = str(request.data.get('source_type') or '').strip()
        confidence = request.data.get('confidence')

        # 3a — Dependency guard: reject if a declared prerequisite has not been accepted
        depends_on = request.data.get('depends_on') or []
        accepted_fix_ids = set(request.data.get('accepted_fix_ids') or [])
        if isinstance(depends_on, list) and depends_on:
            unmet = [dep for dep in depends_on if dep not in accepted_fix_ids]
            if unmet:
                return Response(
                    {'detail': f"Dependency not satisfied: {', '.join(str(d) for d in unmet)}"},
                    status=status.HTTP_409_CONFLICT,
                )

        if not queries or not isinstance(queries, list) or len(queries) == 0:
            return Response(
                {'detail': 'queries must be a non-empty list of SQL strings.'},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if any(not isinstance(q, str) or not q.strip() for q in queries):
            return Response(
                {'detail': 'Each query must be a non-empty SQL string.'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            validate_fix_queries(queries)
        except ValueError as exc:
            audit_log(
                job_id,
                'FIX_EXECUTION_REJECTED',
                {
                    'query_count': len(queries),
                    'error': str(exc),
                    'status_code': 400,
                },
                user_id=str(request.user.id),
            )
            return Response({'detail': str(exc)}, status=status.HTTP_400_BAD_REQUEST)

        # 4 — Execute all queries via isolated execution agent.
        # Build an admin DSN from the app's own DB credentials pointing at the
        # same host/port as the target database.  m6l9k holds CREATEDB so it can
        # run CREATE DATABASE without requiring the job DSN's user to have that role.
        from urllib.parse import urlparse, urlunparse
        from django.conf import settings
        _target = urlparse(connection_string)
        _db = settings.DATABASES['default']
        _admin_dsn = urlunparse((
            'postgresql',
            f"{_db['USER']}:{_db['PASSWORD']}@{_target.hostname}:{_target.port or 5432}",
            '/postgres', '', '', '',
        ))
        _db_name = (_target.path or '').lstrip('/') or None
        user_id_str = str(request.user.id)
        user_email = getattr(request.user, 'email', '')
        email_notif = bool(getattr(request.user, 'email_notifications_enabled', True))

        payload = QueryExecutorAgent().execute(
            queries=queries,
            connection_string=connection_string,
            admin_dsn=_admin_dsn,
            job_id=job_id,
        )

        ex_status = payload.get('status')
        if ex_status == 'success':
            try:
                audit_log(
                    job_id,
                    'FIX_EXECUTED',
                    {
                        'database_id': payload.get('database_id'),
                        'rows_affected': payload.get('rows_affected'),
                        'duration_ms': payload.get('duration_ms'),
                        'query_count': len(queries),
                        'transaction': payload.get('transaction'),
                        'fix_id': fix_id,
                        'card_family': rule_family,
                        'interaction_model': interaction_model,
                        'source_rule': source_rule,
                        'source_type': source_type,
                        'confidence': confidence,
                        'status_code': 200,
                    },
                    user_id=user_id_str,
                )
            except Exception:
                logger.exception('audit_log failed for FIX_EXECUTED job %s', job_id)

            if rule_family and entity and decision:
                try:
                    _record_learning_pattern(request.user, rule_family, entity, decision)
                except Exception:
                    logger.exception('_record_learning_pattern failed for job %s', job_id)
        else:
            try:
                audit_log(
                    job_id,
                    'FIX_EXECUTION_FAILED',
                    {
                        'database_id': payload.get('database_id'),
                        'duration_ms': payload.get('duration_ms'),
                        'query_count': len(queries),
                        'transaction': payload.get('transaction'),
                        'error': payload.get('error'),
                        'fix_id': fix_id,
                        'card_family': rule_family,
                        'interaction_model': interaction_model,
                        'source_rule': source_rule,
                        'source_type': source_type,
                        'confidence': confidence,
                        'status_code': 500,
                    },
                    user_id=user_id_str,
                )
            except Exception:
                logger.exception('audit_log failed for FIX_EXECUTION_FAILED job %s', job_id)

        if email_notif:
            try:
                notify_user_fix_executed(user_email, job_id, ex_status, db_name=_db_name)
            except Exception:
                logger.exception('Failed to send fix execution notification for job %s', job_id)

        if fix_id:
            payload['fix_id'] = fix_id

        return Response(payload, status=status.HTTP_200_OK)


# ──────────────────────────────────────────────
# Admin APM Monitoring
# ──────────────────────────────────────────────

def _to_iso_utc(value: datetime | None) -> str | None:
    if value is None:
        return None

    if dj_tz.is_naive(value):
        value = value.replace(tzinfo=timezone.utc)

    return value.astimezone(timezone.utc).isoformat().replace('+00:00', 'Z')


def _serialize_admin_user(user: User, *, job_count: int = 0) -> dict[str, Any]:
    first_name = str(getattr(user, 'first_name', '') or '').strip()
    last_name = str(getattr(user, 'last_name', '') or '').strip()
    full_name = f'{first_name} {last_name}'.strip()

    return {
        'id': str(user.id),
        'email': user.email,
        'username': user.username,
        'first_name': first_name,
        'last_name': last_name,
        'full_name': full_name,
        'role': user.role,
        'is_staff': bool(user.is_staff),
        'is_active': bool(user.is_active),
        'email_verified': bool(getattr(user, 'email_verified', False)),
        'date_joined': _to_iso_utc(user.date_joined),
        'last_login': _to_iso_utc(user.last_login),
        'job_count': int(job_count),
    }


class AdminUsersView(APIView):
    """GET /api/admin/users/ — paginated admin user listing with filters."""

    permission_classes = [IsAuthenticated]

    def get(self, request):
        admin_error = _require_admin_staff(request)
        if admin_error:
            return admin_error

        page = _coerce_int(request.query_params.get('page'), 1, minimum=1)
        limit = _coerce_int(request.query_params.get('limit'), 25, minimum=1, maximum=100)

        search = str(request.query_params.get('search') or '').strip()
        role = str(request.query_params.get('role') or '').strip().lower()
        status_filter = str(request.query_params.get('status') or '').strip().lower()

        queryset = User.objects.all().order_by('-date_joined').only(
            'id',
            'email',
            'username',
            'first_name',
            'last_name',
            'role',
            'is_staff',
            'is_active',
            'email_verified',
            'date_joined',
            'last_login',
        )

        if search:
            queryset = queryset.filter(
                Q(email__icontains=search)
                | Q(username__icontains=search)
                | Q(first_name__icontains=search)
                | Q(last_name__icontains=search)
            )

        if role in {'admin', 'user'}:
            queryset = queryset.filter(role=role)

        if status_filter == 'active':
            queryset = queryset.filter(is_active=True)
        elif status_filter in {'disabled', 'inactive'}:
            queryset = queryset.filter(is_active=False)

        total = queryset.count()
        offset = (page - 1) * limit
        rows = list(queryset[offset:offset + limit])

        user_ids = [str(user.id) for user in rows]
        job_counts = {
            str(entry['user_id']): int(entry['job_count'])
            for entry in (
                Job.objects
                .filter(user_id__in=user_ids)
                .values('user_id')
                .annotate(job_count=Count('id'))
            )
        }

        items = [
            _serialize_admin_user(user, job_count=job_counts.get(str(user.id), 0))
            for user in rows
        ]

        total_pages = max(1, math.ceil(total / limit)) if total else 1
        return Response(
            {
                'items': items,
                'page': page,
                'limit': limit,
                'total': total,
                'total_pages': total_pages,
            }
        )


class AdminUserDetailView(APIView):
    """PATCH /api/admin/users/<user_id>/ — update role and/or active flag."""

    permission_classes = [IsAuthenticated]

    def patch(self, request, user_id):
        admin_error = _require_admin_staff(request)
        if admin_error:
            return admin_error

        target_user = User.objects.filter(id=user_id).first()
        if not target_user:
            return Response({'error': 'User not found.'}, status=status.HTTP_404_NOT_FOUND)

        updates: list[str] = []
        role_error = self._apply_role_update(request=request, target_user=target_user, updates=updates)
        if role_error:
            return role_error

        active_error = self._apply_active_update(request=request, target_user=target_user, updates=updates)
        if active_error:
            return active_error

        if not updates:
            return Response(
                {'error': 'Provide at least one field to update: role or is_active.'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        target_user.save(update_fields=sorted(set(updates)))
        job_count = Job.objects.filter(user_id=str(target_user.id)).count()

        audit_log(
            '',
            'ADMIN_USER_UPDATED',
            {
                'target_user_id': str(target_user.id),
                'target_user_email': target_user.email,
                'updated_fields': sorted(set(updates)),
                'role': target_user.role,
                'is_active': bool(target_user.is_active),
                'status_code': 200,
            },
            user_id=str(request.user.id),
        )

        return Response({'user': _serialize_admin_user(target_user, job_count=job_count)})

    def _apply_role_update(self, *, request, target_user: User, updates: list[str]) -> Response | None:
        if 'role' not in request.data:
            return None

        role = str(request.data.get('role') or '').strip().lower()
        if role not in {'admin', 'user'}:
            return Response({'error': 'role must be either "admin" or "user".'}, status=status.HTTP_400_BAD_REQUEST)

        if str(target_user.id) == str(request.user.id) and role != 'admin':
            return Response(
                {'error': 'You cannot demote your own admin account.'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        target_user.role = role
        target_user.is_staff = role == 'admin'
        updates.extend(['role', 'is_staff'])
        return None

    def _apply_active_update(self, *, request, target_user: User, updates: list[str]) -> Response | None:
        if 'is_active' not in request.data:
            return None

        is_active_raw = request.data.get('is_active')
        if not isinstance(is_active_raw, bool):
            return Response({'error': 'is_active must be a boolean.'}, status=status.HTTP_400_BAD_REQUEST)

        if str(target_user.id) == str(request.user.id) and is_active_raw is False:
            return Response(
                {'error': 'You cannot disable your own account.'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        target_user.is_active = is_active_raw
        updates.append('is_active')
        return None

class AdminStatsView(APIView):
    """GET /api/admin/stats/ — aggregate KPI cards and active-jobs snapshot."""

    permission_classes = [IsAuthenticated]

    def get(self, request):
        admin_error = _require_admin_staff(request)
        if admin_error:
            return admin_error

        now = dj_tz.now()
        today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)

        total_users = User.objects.count()
        total_jobs = Job.objects.count()
        jobs_today = Job.objects.filter(created_at__gte=today_start).count()
        failed_jobs = Job.objects.filter(status='failed').count()
        failed_jobs_today = Job.objects.filter(
            created_at__gte=today_start,
            status='failed',
        ).count()

        active_jobs_qs = Job.objects.filter(
            Q(status__in=list(_ADMIN_ACTIVE_JOB_STATUSES)) | Q(status__startswith='running_')
        ).only('id', 'status', 'updated_at', 'user_id', 'mode').order_by('-updated_at')
        active_jobs_count = active_jobs_qs.count()
        active_jobs_sample = list(active_jobs_qs[:25])

        active_user_ids = [entry.user_id for entry in active_jobs_sample if entry.user_id]
        active_user_emails = {
            str(user.id): user.email
            for user in User.objects.filter(id__in=active_user_ids).only('id', 'email')
        }
        active_jobs_details = [
            {
                'job_id': entry.id,
                'status': entry.status,
                'mode': entry.mode,
                'updated_at': entry.updated_at.isoformat().replace('+00:00', 'Z') if entry.updated_at else None,
                'user_email': active_user_emails.get(str(entry.user_id)),
            }
            for entry in active_jobs_sample
            if _is_active_job_status(entry.status)
        ]

        duration_event_types = [
            'ANALYSIS_COMPLETED',
            'DB_ANALYSIS_COMPLETED',
            'FIX_EXECUTED',
        ]
        duration_logs = AuditLog.objects.filter(
            created_at__gte=now - timedelta(days=7),
            event_type__in=duration_event_types,
        ).only('event_type', 'payload')

        duration_buckets: dict[str, list[float]] = {}
        for entry in duration_logs.iterator():
            payload = entry.payload if isinstance(entry.payload, dict) else {}
            raw_duration = payload.get('duration_ms')
            try:
                duration_value = float(raw_duration)
            except (TypeError, ValueError):
                continue

            if duration_value < 0:
                continue
            duration_buckets.setdefault(entry.event_type, []).append(duration_value)

        all_durations = [duration for values in duration_buckets.values() for duration in values]
        average_response_time_ms = round(sum(all_durations) / len(all_durations), 2) if all_durations else 0.0

        slowest_endpoint = None
        slowest_endpoint_duration_ms = 0.0
        if duration_buckets:
            slowest_endpoint, durations = max(
                duration_buckets.items(),
                key=lambda item: sum(item[1]) / len(item[1]),
            )
            slowest_endpoint_duration_ms = round(sum(durations) / len(durations), 2)

        classified_events = AuditLog.objects.filter(
            created_at__gte=now - timedelta(hours=24)
        ).only('event_type', 'payload')
        classified_total = 0
        error_events = 0
        for entry in classified_events.iterator():
            classification = _classify_audit_event(entry.event_type, entry.payload)
            if classification is None:
                continue
            classified_total += 1
            if classification == 'error':
                error_events += 1

        error_rate = round((error_events * 100.0) / classified_total, 2) if classified_total else 0.0

        fixes_applied_today = AuditLog.objects.filter(
            event_type='FIX_EXECUTED',
            created_at__gte=today_start,
        ).count()

        health_status = 'healthy'
        if error_rate >= 20 or failed_jobs_today >= 10:
            health_status = 'critical'
        elif error_rate >= 10 or failed_jobs_today >= 3:
            health_status = 'warning'

        return Response(
            {
                'health_status': health_status,
                'total_users': total_users,
                'total_jobs': total_jobs,
                'jobs_today': jobs_today,
                'active_jobs': active_jobs_count,
                'active_jobs_details': active_jobs_details,
                'average_response_time': average_response_time_ms,
                'average_response_time_ms': average_response_time_ms,
                'slowest_endpoint': slowest_endpoint,
                'slowest_endpoint_duration_ms': slowest_endpoint_duration_ms,
                'failed_jobs': failed_jobs,
                'failed_jobs_today': failed_jobs_today,
                'error_rate': error_rate,
                'fixes_applied_today': fixes_applied_today,
                'last_updated': now.isoformat().replace('+00:00', 'Z'),
            }
        )


class AdminPipelinePerformanceView(APIView):
    """GET /api/admin/pipeline-performance/ — step-level timings and success rates."""

    permission_classes = [IsAuthenticated]

    def get(self, request):
        admin_error = _require_admin_staff(request)
        if admin_error:
            return admin_error

        now = dj_tz.now()
        window_days = _coerce_int(request.query_params.get('days'), 7, minimum=1, maximum=30)
        since = now - timedelta(days=window_days)

        step_metrics: dict[str, dict[str, Any]] = {}
        run_metrics: dict[str, dict[str, Any]] = {}
        measured_logs = AuditLog.objects.filter(
            created_at__gte=since,
            event_type__in=['PIPELINE_STEP_COMPLETED', 'PIPELINE_STEP_FAILED'],
        ).only('event_type', 'payload')

        for entry in measured_logs.iterator():
            payload = entry.payload if isinstance(entry.payload, dict) else {}
            step = str(payload.get('step') or '').strip()
            if not step:
                continue

            metric = step_metrics.setdefault(
                step,
                {
                    'durations': [],
                    'success': 0,
                    'total': 0,
                    'source': 'measured',
                },
            )

            metric['total'] += 1
            classification = _classify_audit_event(entry.event_type, payload)
            if classification != 'error' and entry.event_type == 'PIPELINE_STEP_COMPLETED':
                metric['success'] += 1

            try:
                duration_ms = float(payload.get('duration_ms'))
            except (TypeError, ValueError):
                duration_ms = None

            if duration_ms is not None and duration_ms >= 0:
                metric['durations'].append(duration_ms)

            pipeline_run_id = str(payload.get('pipeline_run_id') or '').strip()
            if not pipeline_run_id:
                continue

            run_metric = run_metrics.setdefault(
                pipeline_run_id,
                {
                    'duration_ms': 0.0,
                    'timed_steps': 0,
                    'has_failure': False,
                },
            )
            if duration_ms is not None and duration_ms >= 0:
                run_metric['duration_ms'] += duration_ms
                run_metric['timed_steps'] += 1

            if entry.event_type == 'PIPELINE_STEP_FAILED' or classification == 'error':
                run_metric['has_failure'] = True

        uses_estimation = False

        step_label_map = {key: label for key, label in _PIPELINE_STEP_DEFINITIONS}
        canonical_steps = [key for key, _label in _PIPELINE_STEP_DEFINITIONS]
        extra_steps = sorted([key for key in step_metrics.keys() if key not in canonical_steps])
        ordered_steps = canonical_steps + extra_steps

        step_rows = []
        for step in ordered_steps:
            metric = step_metrics.get(
                step,
                {
                    'durations': [],
                    'success': 0,
                    'total': 0,
                    'source': 'none',
                },
            )
            durations = metric['durations']
            sample_count = len(durations)
            average_duration_ms = round(sum(durations) / sample_count, 2) if sample_count else 0.0
            min_duration_ms = round(min(durations), 2) if sample_count else 0.0
            max_duration_ms = round(max(durations), 2) if sample_count else 0.0
            success_rate = round((metric['success'] * 100.0) / metric['total'], 2) if metric['total'] else 0.0

            step_rows.append(
                {
                    'step': step,
                    'label': step_label_map.get(step, step.replace('_', ' ').title()),
                    'average_duration_ms': average_duration_ms,
                    'min_duration_ms': min_duration_ms,
                    'max_duration_ms': max_duration_ms,
                    'sample_count': sample_count,
                    'success_rate': success_rate,
                    'source': metric['source'],
                }
            )

        run_totals = [
            entry['duration_ms']
            for entry in run_metrics.values()
            if entry.get('timed_steps', 0) > 0
        ]
        total_sample_count = len(run_totals)
        total_average_duration_ms = round(sum(run_totals) / total_sample_count, 2) if total_sample_count else 0.0
        total_min_duration_ms = round(min(run_totals), 2) if total_sample_count else 0.0
        total_max_duration_ms = round(max(run_totals), 2) if total_sample_count else 0.0
        successful_runs = sum(
            1
            for entry in run_metrics.values()
            if entry.get('timed_steps', 0) > 0 and not entry.get('has_failure', False)
        )
        total_success_rate = round((successful_runs * 100.0) / total_sample_count, 2) if total_sample_count else 0.0

        step_rows.append(
            {
                'step': 'total_pipeline',
                'label': 'Total Pipeline',
                'average_duration_ms': total_average_duration_ms,
                'min_duration_ms': total_min_duration_ms,
                'max_duration_ms': total_max_duration_ms,
                'sample_count': total_sample_count,
                'success_rate': total_success_rate,
                'source': 'measured' if total_sample_count else 'none',
            }
        )

        return Response(
            {
                'window_days': window_days,
                'uses_estimation': uses_estimation,
                'steps': step_rows,
                'generated_at': now.isoformat().replace('+00:00', 'Z'),
            }
        )


class AdminAuditLogView(APIView):
    """GET /api/admin/audit-log/ — filtered and paginated audit events."""

    permission_classes = [IsAuthenticated]

    def get(self, request):
        admin_error = _require_admin_staff(request)
        if admin_error:
            return admin_error

        page = _coerce_int(request.query_params.get('page'), 1, minimum=1)
        limit = _coerce_int(request.query_params.get('limit'), 25, minimum=1, maximum=200)

        event_filter = str(request.query_params.get('event') or '').strip()
        job_id_filter = str(request.query_params.get('job_id') or '').strip()
        user_email_filter = str(request.query_params.get('user_email') or '').strip()
        date_from_raw = request.query_params.get('date_from')
        date_to_raw = request.query_params.get('date_to')

        date_from = _parse_datetime_filter(date_from_raw)
        date_to = _parse_datetime_filter(date_to_raw, end_of_day=True)
        if date_from_raw and date_from is None:
            return Response({'error': 'Invalid date_from format. Use ISO datetime or YYYY-MM-DD.'}, status=400)
        if date_to_raw and date_to is None:
            return Response({'error': 'Invalid date_to format. Use ISO datetime or YYYY-MM-DD.'}, status=400)

        queryset = AuditLog.objects.all().order_by('-created_at')

        if event_filter:
            queryset = queryset.filter(event_type__icontains=event_filter)
        if job_id_filter:
            queryset = queryset.filter(job_id__icontains=job_id_filter)
        if date_from:
            queryset = queryset.filter(created_at__gte=date_from)
        if date_to:
            queryset = queryset.filter(created_at__lte=date_to)

        if user_email_filter:
            matching_user_ids = [
                str(uid)
                for uid in User.objects.filter(email__icontains=user_email_filter).values_list('id', flat=True)
            ]
            if matching_user_ids:
                matching_job_ids = list(
                    str(job_id)
                    for job_id in Job.objects.filter(user_id__in=matching_user_ids).values_list('id', flat=True)
                )
                if matching_job_ids:
                    queryset = queryset.filter(job_id__in=matching_job_ids)
                else:
                    queryset = queryset.none()
            else:
                queryset = queryset.none()

        total = queryset.count()
        offset = (page - 1) * limit
        rows = list(queryset[offset:offset + limit])

        row_job_ids = [entry.job_id for entry in rows if entry.job_id]
        job_owner_map = {
            str(row['id']): str(row['user_id'])
            for row in Job.objects.filter(id__in=row_job_ids).values('id', 'user_id')
        }
        owner_user_ids = [uid for uid in set(job_owner_map.values()) if uid]
        owner_email_map = {
            str(user.id): user.email
            for user in User.objects.filter(id__in=owner_user_ids).only('id', 'email')
        }

        items = []
        for entry in rows:
            payload = entry.payload if isinstance(entry.payload, dict) else {}
            owner_user_id = job_owner_map.get(str(entry.job_id or ''))
            user_email = owner_email_map.get(owner_user_id or '')
            classification = _classify_audit_event(entry.event_type, payload) or 'unknown'

            items.append(
                {
                    'id': entry.id,
                    'timestamp': entry.created_at.isoformat().replace('+00:00', 'Z'),
                    'event': entry.event_type,
                    'job_id': entry.job_id,
                    'user_email': user_email,
                    'status': classification,
                    'status_code': _extract_status_code(payload),
                    'detail': json.dumps(payload, ensure_ascii=True, sort_keys=True),
                    'payload': payload,
                }
            )

        total_pages = max(1, math.ceil(total / limit)) if total else 1
        return Response(
            {
                'items': items,
                'page': page,
                'limit': limit,
                'total': total,
                'total_pages': total_pages,
            }
        )


class AdminErrorTimelineView(APIView):
    """GET /api/admin/error-timeline/ — 24h grouped success/error counts."""

    permission_classes = [IsAuthenticated]

    def get(self, request):
        admin_error = _require_admin_staff(request)
        if admin_error:
            return admin_error

        hours = _coerce_int(request.query_params.get('hours'), 24, minimum=1, maximum=168)
        current_hour = dj_tz.now().astimezone(timezone.utc).replace(minute=0, second=0, microsecond=0)
        start_hour = current_hour - timedelta(hours=hours - 1)

        buckets: dict[str, dict[str, Any]] = {}
        cursor = start_hour
        while cursor <= current_hour:
            key = cursor.isoformat().replace('+00:00', 'Z')
            buckets[key] = {
                'hour': key,
                'error_count': 0,
                'success_count': 0,
            }
            cursor += timedelta(hours=1)

        timeline_logs = AuditLog.objects.filter(
            created_at__gte=start_hour,
            created_at__lt=current_hour + timedelta(hours=1),
        ).only('created_at', 'event_type', 'payload')

        for entry in timeline_logs.iterator():
            bucket_hour = entry.created_at.astimezone(timezone.utc).replace(minute=0, second=0, microsecond=0)
            bucket_key = bucket_hour.isoformat().replace('+00:00', 'Z')
            bucket = buckets.get(bucket_key)
            if not bucket:
                continue

            classification = _classify_audit_event(entry.event_type, entry.payload)
            if classification == 'error':
                bucket['error_count'] += 1
            elif classification == 'success':
                bucket['success_count'] += 1

        timeline = [buckets[key] for key in sorted(buckets.keys())]
        return Response(
            {
                'hours': hours,
                'timeline': timeline,
            }
        )


class AdminTestEmailView(APIView):
    """POST /api/admin/test-email/ — sends SMTP diagnostic mail to ADMIN_EMAIL."""

    permission_classes = [IsAuthenticated]

    def post(self, request):
        admin_error = _require_admin_staff(request)
        if admin_error:
            return admin_error

        admin_email = str(getattr(settings, 'ADMIN_EMAIL', '') or '').strip()
        if not admin_email:
            return Response(
                {
                    'ok': False,
                    'error': 'ADMIN_EMAIL is not configured.',
                    'code': 'admin_email_missing',
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        now = dj_tz.now()
        subject = f'QualityLab Admin Diagnostic - {now.strftime("%Y-%m-%d %H:%M:%S UTC")}'
        plain_message = (
            'This is a test email from the QualityLab admin dashboard. '
            'Your SMTP configuration is working.'
        )
        html_message = (
            '<p>This is a test email from the <strong>QualityLab admin dashboard</strong>.</p>'
            '<p>Your SMTP configuration is working.</p>'
        )

        try:
            sent_count = send_mail(
                subject=subject,
                message=plain_message,
                from_email=settings.DEFAULT_FROM_EMAIL,
                recipient_list=[admin_email],
                fail_silently=False,
                html_message=html_message,
            )

            if sent_count != 1:
                raise RuntimeError(f'Expected sent_count=1, got {sent_count}.')

            audit_log(
                '',
                'ADMIN_TEST_EMAIL_SENT',
                {
                    'recipient': admin_email,
                    'status_code': 200,
                },
                user_id=str(request.user.id),
            )
            return Response(
                {
                    'ok': True,
                    'recipient': admin_email,
                    'sent_count': sent_count,
                    'message': 'Test email sent successfully.',
                }
            )

        except Exception as exc:
            audit_log(
                '',
                'ADMIN_TEST_EMAIL_FAILED',
                {
                    'recipient': admin_email,
                    'status_code': 500,
                    'error': str(exc),
                },
                user_id=str(request.user.id),
            )
            logger.exception('Failed to send admin test email')
            return Response(
                {
                    'ok': False,
                    'error': 'Failed to send admin test email.',
                    'detail': str(exc),
                },
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )


# ──────────────────────────────────────────────
# AI-Generated Guidelines
# ──────────────────────────────────────────────

class GuidelinesView(APIView):
    """
    GET /api/guidelines/<jobId>

    Returns the AI-generated guideline text produced by the GuidelineGenerator
    for each pipeline phase, stored during the analysis run.

        Lookup order:
            1. Per-run DB entries from `job.guidelines`

        Returns a partial map when analysis is still running instead of 404,
        so the frontend can progressively reveal available guideline stages.

        Response shape:
            {
                "schema_integrity_validator": "...",
                "column_validator":           "...",
                "cross_row_validator":        "...",
                "row_level_validator":        "...",
                "data_fix_planner":           "..."
            }
    """
    permission_classes = [IsAuthenticated]

    def get(self, request, jobId):
        job = find_owned_job(jobId, request.user)
        if not job:
            return Response(
                {'detail': 'Job not found or access denied.'},
                status=status.HTTP_404_NOT_FOUND,
            )

        # ── 1. Primary: per-run DB entry ───────────────────────────────
        persisted = dict(get_job_guidelines(jobId) or {})

        merged = persisted

        ordered = {}
        for key in _GUIDELINE_KEYS:
            text = (merged.get(key) or '').strip()
            if text:
                ordered[key] = text

        # Preserve any additional non-empty keys without dropping them.
        for key, value in merged.items():
            if key in ordered:
                continue
            if isinstance(value, str) and value.strip():
                ordered[key] = value

        return Response(ordered)


class LangfuseProxyView(APIView):
    """
    GET /api/admin/langfuse/status/  → {enabled, host}
    GET /api/admin/langfuse/traces/  → proxied /api/public/traces filtered to qualitylab-pipeline
    """

    permission_classes = [IsAuthenticated]

    _LANGFUSE_TIMEOUT = 10

    def get(self, request, endpoint='status'):
        admin_error = _require_admin_staff(request)
        if admin_error:
            return admin_error

        public_key = os.environ.get('LANGFUSE_PUBLIC_KEY', '').strip()
        secret_key = os.environ.get('LANGFUSE_SECRET_KEY', '').strip()
        host = os.environ.get('LANGFUSE_HOST', 'https://cloud.langfuse.com').rstrip('/')

        if endpoint == 'status':
            return Response({
                'enabled': bool(public_key and secret_key),
                'host': host if (public_key and secret_key) else None,
            })

        if endpoint == 'traces':
            if not (public_key and secret_key):
                return Response({'enabled': False, 'data': [], 'meta': {}})

            page = _coerce_int(request.query_params.get('page', 1), default=1, minimum=1)
            limit = _coerce_int(request.query_params.get('limit', 10), default=10, minimum=1, maximum=50)

            try:
                resp = http_requests.get(
                    f'{host}/api/public/traces',
                    auth=(public_key, secret_key),
                    params={'page': page, 'limit': limit, 'name': 'qualitylab-pipeline'},
                    timeout=self._LANGFUSE_TIMEOUT,
                )
                resp.raise_for_status()
                return Response(resp.json())
            except http_requests.exceptions.Timeout:
                return Response({'error': 'Langfuse request timed out.'}, status=status.HTTP_504_GATEWAY_TIMEOUT)
            except http_requests.exceptions.RequestException as exc:
                logger.warning('LangfuseProxyView: request failed: %s', exc)
                return Response({'error': 'Failed to reach Langfuse.', 'detail': str(exc)}, status=status.HTTP_502_BAD_GATEWAY)

        return Response({'error': 'Unknown endpoint.'}, status=status.HTTP_404_NOT_FOUND)
