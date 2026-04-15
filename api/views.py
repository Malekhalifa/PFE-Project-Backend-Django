"""
API views for jobs, uploads, analysis, cleaning, and raw data.

Every endpoint matches the Node.js backend's API surface exactly,
so the Angular frontend requires only a base URL change.

All views require authentication (CookieJWTAuthentication) and
enforce job ownership via find_owned_job().
"""
import math
import os
import secrets
import uuid
import logging
import json
import requests as http_requests
from datetime import datetime, time, timedelta, timezone
from typing import Any

import pandas as pd
from django.conf import settings
from django.core.mail import send_mail
from django.db.models import Q
from django.http import FileResponse
from django.utils import timezone as dj_tz
from django.utils.dateparse import parse_date, parse_datetime
from rest_framework import status
from rest_framework.parsers import MultiPartParser
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.throttling import UserRateThrottle
from rest_framework.views import APIView

from api.models import AuditLog, Job, audit_log
from api.db import (
    create_job,
    get_job,
    get_jobs_for_user,
    get_analysis_result,
    save_analysis_result,
    get_cleaning_result,
    get_analysis_job_ids,
    get_cleaning_job_ids,
    get_chat_job_ids,
    get_chat_messages,
    save_chat_message,
    delete_jobs,
    update_job_share_token,
    update_job_connection_string,
    get_job_by_share_token,
    update_job_status,
    get_job_guidelines,
)
from api.permissions import find_owned_job
from api.serializers import CleanRequestSerializer, JobDeleteSerializer
from agents.query_executor_agent import QueryExecutorAgent
from agents.reporting import synthesize_fixes_from_prior_reports
from services.analysis_service import compute_row_outliers, to_native
from services.notification_service import notify_user_fix_executed
from services.postgres_connection import (
    looks_like_postgres_dsn,
    redact_postgres_dsn,
    test_postgres_connection,
)
from tasks.csv_tasks import analyze_csv_task, clean_csv_task
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


_ADMIN_ACTIVE_JOB_STATUSES = {'starting', 'running', 'cleaning'}
_TERMINAL_ACTIVE_PREFIX_EXCLUSIONS = {'cleaning_completed', 'cleaning_failed'}
_PIPELINE_STEP_DEFINITIONS = [
    ('guideline_gen_schema', 'Guideline Generation - Schema'),
    ('schema_validation', 'Schema Validation'),
    ('guideline_gen_column', 'Guideline Generation - Column'),
    ('column_validation', 'Column Validation'),
    ('guideline_gen_row_level', 'Guideline Generation - Row Level'),
    ('row_level_validation', 'Row Level Validation'),
    ('guideline_gen_cross_row', 'Guideline Generation - Cross Row'),
    ('cross_row_validation', 'Cross Row Validation'),
    ('guideline_gen_fix_planner', 'Guideline Generation - Fix Planner'),
    ('fix_planner', 'Fix Planner'),
    ('saving_guidelines', 'Saving Guidelines'),
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
    'column_validator',
    'row_level_validator',
    'cross_row_validator',
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
            fix_cards = synthesize_fixes_from_prior_reports(prior_reports)

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


# ──────────────────────────────────────────────
# Upload
# ──────────────────────────────────────────────

class UploadView(APIView):
    """
    POST /api/upload — upload CSV file, create job.

    Matches Node.js: saves file to uploads/, creates Job in MongoDB,
    writes audit log, returns { job_id }.
    """
    permission_classes = [IsAuthenticated]
    parser_classes = [MultiPartParser]

    def post(self, request):
        unverified = _require_verified_email(request)
        if unverified:
            return unverified

        uploaded_file = request.FILES.get('file')
        if not uploaded_file:
            return Response({'error': 'No file uploaded'}, status=status.HTTP_400_BAD_REQUEST)

        # Save file to disk with UUID filename (matches Node.js multer pattern)
        file_ext = os.path.splitext(uploaded_file.name)[1] or '.csv'
        file_uuid = str(uuid.uuid4())
        filename = f'{file_uuid}{file_ext}'
        file_path = os.path.join(str(settings.UPLOAD_DIR), filename)

        with open(file_path, 'wb+') as dest:
            for chunk in uploaded_file.chunks():
                dest.write(chunk)

        file_size = os.path.getsize(file_path)
        mode = 'large' if file_size > 50 * 1024 * 1024 else 'normal'  # 50MB threshold

        # Create job in MongoDB
        user_id = str(request.user.id)
        job_id = create_job(
            user_id=user_id,
            file_name=uploaded_file.name,
            file_path=file_path,
            mode=mode,
        )

        # Audit logs
        audit_log(job_id, 'JOB_CREATED', {'user_id': user_id}, user_id=user_id)
        audit_log(job_id, 'FILE_UPLOADED', {
            'fileName': uploaded_file.name,
            'size': file_size,
            'mode': mode,
        }, user_id=user_id)

        return Response({'job_id': job_id})


# ──────────────────────────────────────────────
# Analysis
# ──────────────────────────────────────────────

class AnalyzeView(APIView):
    """
    POST /api/analyze/<jobId> — start analysis (fire-and-forget Celery task).
    """
    permission_classes = [IsAuthenticated]

    def post(self, request, jobId):
        unverified = _require_verified_email(request)
        if unverified:
            return unverified

        job = find_owned_job(jobId, request.user)
        if not job:
            return Response(
                {'detail': 'No uploaded file found for this job.'},
                status=status.HTTP_404_NOT_FOUND,
            )

        # Dispatch Celery task (fire-and-forget)
        analyze_csv_task.delay(
            job_id=jobId,
            file_path=job['file_path'],
            mode=job.get('mode', 'normal'),
            options={},
        )

        audit_log(jobId, 'ANALYSIS_ENQUEUED', {}, user_id=str(request.user.id))
        return Response({'message': 'analysis started'})


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
    Body: { "connection_string": "...", "domain_rules": [...] }
    """
    permission_classes = [IsAuthenticated]
    throttle_classes = [AnalyzePostgresUserRateThrottle]

    def post(self, request):
        unverified = _require_verified_email(request)
        if unverified:
            return unverified

        conn_str = request.data.get('connection_string')
        domain_rules = request.data.get('domain_rules', [])
        
        if not conn_str:
            return Response({'error': 'connection_string is required'}, status=status.HTTP_400_BAD_REQUEST)

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

        user_id = str(request.user.id)
        
        # Create a job tracking entry
        job_id = create_job(
            user_id=user_id,
            file_name="PostgreSQL Connection",
            file_path="postgres://<hidden>",
            mode="agentic_postgres",
        )

        # Persist the DSN in the job document so the cleaning task
        # can retrieve it later without needing it re-passed.
        update_job_connection_string(job_id, conn_str)

        analyze_postgres_task.delay(
            job_id=job_id,
            connection_string=conn_str,
            domain_rules=domain_rules,
        )

        audit_log(job_id, 'DB_ANALYSIS_ENQUEUED', {}, user_id=user_id)
        return Response({'job_id': job_id, 'message': 'Postgres analysis started'})


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

        # If it's already the new CSV format natively (has dataset_summary)
        # OR if it's an agentic postgres format (has schema_analysis)
        if 'dataset_summary' in result or 'schema_analysis' in result:
            return Response(_sanitize_for_json(result))

        # Otherwise, map the legacy CSV format to the new format
        meta = result.get('meta', {})
        qr = _sanitize_for_json(result.get('quality_report', {}))
        cleaned = result.get('cleaned_data', {})

        response_data = {
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

        if 'schema_analysis' in result:
            response_data['schema_analysis'] = result['schema_analysis']
        if 'column_validator_analysis' in result:
            response_data['column_validator_analysis'] = result['column_validator_analysis']
        if 'cross_row_analysis' in result:
            response_data['cross_row_analysis'] = result['cross_row_analysis']
        if 'row_level_analysis' in result:
            response_data['row_level_analysis'] = result['row_level_analysis']

        return Response(response_data)


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

        # If already in native CSV-v2 format or agentic PostgreSQL format,
        # return as-is (sanitized) so all generated keys are preserved.
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


class RawDataView(APIView):
    """
    GET /api/raw/<jobId> — get raw CSV data as { header, rows }.

    In the Node.js backend this was proxied to Python. Now we do it directly.
    """
    permission_classes = [IsAuthenticated]

    def get(self, request, jobId):
        job = find_owned_job(jobId, request.user)
        if not job or not job.get('file_path'):
            return Response(
                {'detail': 'No data found for this job.'},
                status=status.HTTP_404_NOT_FOUND,
            )

        file_path = job['file_path']
        if not os.path.exists(file_path):
            return Response(
                {'detail': 'File not found on disk'},
                status=status.HTTP_404_NOT_FOUND,
            )

        df = pd.read_csv(file_path)
        df = df.replace(
            {pd.NA: None, float('nan'): None, float('inf'): None, float('-inf'): None}
        )
        df = df.where(pd.notna(df), None)

        return Response(_sanitize_for_json({
            'header': df.columns.tolist(),
            'rows': df.values.tolist(),
        }))


class RawWithOutliersView(APIView):
    """
    GET /api/raw_with_outliers/<jobId> — get raw CSV with row-level outlier metadata.

    Returns { header, rows: [{ values, is_outlier, outlier_score, outlier_severity }] }.
    """
    permission_classes = [IsAuthenticated]

    def get(self, request, jobId):
        job = find_owned_job(jobId, request.user)
        if not job or not job.get('file_path'):
            return Response(
                {'detail': 'No data found for this job.'},
                status=status.HTTP_404_NOT_FOUND,
            )

        file_path = job['file_path']
        if not os.path.exists(file_path):
            return Response(
                {'detail': 'File not found on disk'},
                status=status.HTTP_404_NOT_FOUND,
            )

        df = pd.read_csv(file_path)
        df = df.replace(
            {pd.NA: None, float('nan'): None, float('inf'): None, float('-inf'): None}
        )
        df = df.where(pd.notna(df), None)

        header = df.columns.tolist()
        rows = df.values.tolist()
        enriched_rows = compute_row_outliers(header, rows)

        return Response({
            'header': header,
            'rows': _sanitize_for_json(to_native(enriched_rows)),
        })


# ──────────────────────────────────────────────
# Cleaning
# ──────────────────────────────────────────────

class CleanView(APIView):
    """
    POST /api/clean/<jobId> — start cleaning (fire-and-forget Celery task).

    Body: { mode: "auto"|"manual", rules?: { ... } }
    """
    permission_classes = [IsAuthenticated]

    def post(self, request, jobId):
        unverified = _require_verified_email(request)
        if unverified:
            return unverified

        job = find_owned_job(jobId, request.user)
        if not job:
            return Response({'error': 'Job not found'}, status=status.HTTP_404_NOT_FOUND)

        # Parse cleaning options
        mode = request.data.get('mode', 'auto')
        rules = request.data.get('rules', {})

        # Dispatch Celery task (fire-and-forget)
        clean_csv_task.delay(
            job_id=jobId,
            file_path=job['file_path'],
            mode=mode,
            rules=rules,
            options={},
        )

        audit_log(jobId, 'CLEANING_ENQUEUED', {'mode': mode}, user_id=str(request.user.id))
        return Response({'job_id': jobId, 'status': 'cleaning_started'})


class CleanedFileView(APIView):
    """
    GET /api/cleaned/<jobId> — download cleaned CSV file.
    """
    permission_classes = [IsAuthenticated]

    def get(self, request, jobId):
        job = find_owned_job(jobId, request.user)
        if not job or not job.get('cleaned_file_path'):
            return Response(
                {'error': 'Cleaned file not found'},
                status=status.HTTP_404_NOT_FOUND,
            )

        cleaned_path = job['cleaned_file_path']
        if not os.path.exists(cleaned_path):
            return Response(
                {'error': 'File missing on disk'},
                status=status.HTTP_404_NOT_FOUND,
            )

        return FileResponse(
            open(cleaned_path, 'rb'),
            content_type='text/csv',
            as_attachment=True,
            filename=f'{jobId}-cleaned.csv',
        )


class CleaningResultView(APIView):
    """
    GET /api/cleaning-result/<jobId> — get cleaning result metadata.
    """
    permission_classes = [IsAuthenticated]

    def get(self, request, jobId):
        job = find_owned_job(jobId, request.user)
        if not job:
            return Response({'error': 'Not found'}, status=status.HTTP_404_NOT_FOUND)

        result = get_cleaning_result(jobId)
        if not result:
            # Return null instead of 404 so the frontend catchError isn't triggered
            # and Chrome doesn't log a red console error
            return Response(None)

        # Remove MongoDB's internal _id field before returning
        result.pop('_id', None)
        return Response(_sanitize_for_json(result))


class CleaningResultExportView(APIView):
    """
    GET /api/cleaning-result/<jobId>/export — download cleaning report.

    Returns structured report matching Node.js export format.
    """
    permission_classes = [IsAuthenticated]

    def get(self, request, jobId):
        job = find_owned_job(jobId, request.user)
        if not job:
            return Response({'error': 'Not found'}, status=status.HTTP_404_NOT_FOUND)

        result = get_cleaning_result(jobId)
        if not result:
            return Response(
                {'error': 'No cleaning result found'},
                status=status.HTTP_404_NOT_FOUND,
            )

        summary = result.get('summary', {})
        rules_applied = result.get('rules_applied', [])
        rows_before = summary.get('rows_before', 0)
        rows_removed = summary.get('rows_removed', 0)

        report = {
            'meta': {
                'job_id': jobId,
                'file_name': job.get('file_name'),
                'file_path': job.get('file_path'),
                'cleaned_file_path': job.get('cleaned_file_path'),
                'cleaning_mode': result.get('mode', 'auto'),
                'cleaned_at': (
                    result.get('updated_at') or result.get('created_at') or None
                ),
                'version': '1.0',
            },
            'summary': {
                'rows_before': rows_before,
                'rows_after': summary.get('rows_after'),
                'rows_removed': rows_removed,
                'removal_rate': (
                    round(rows_removed / rows_before, 4)
                    if rows_before else None
                ),
            },
            'rules_applied': [
                {
                    'rule': r.get('rule'),
                    'description': r.get('description'),
                    'rows_removed': r.get('rows_removed'),
                    'columns': r.get('columns'),
                }
                for r in rules_applied
            ],
            'rules_summary': {
                'total_rules_applied': len(rules_applied),
                'rules_list': [r.get('rule') for r in rules_applied if r.get('rule')],
            },
        }

        return Response(report)


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
        job_ids = [j['_id'] for j in jobs]

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

        # Batch-check which jobs have analysis/cleaning/chat results
        analysis_set = get_analysis_job_ids(job_ids) if job_ids else set()
        cleaning_set = get_cleaning_job_ids(job_ids) if job_ids else set()
        chat_set = get_chat_job_ids(job_ids) if job_ids else set()

        result = [
            {
                '_id': j['_id'],
                'file_name': j.get('file_name', 'unknown'),
                'status': j.get('status'),
                'mode': j.get('mode'),
                'created_at': j.get('created_at'),
                'has_analysis': j['_id'] in analysis_set,
                'has_cleaning': j['_id'] in cleaning_set,
                'has_chat': j['_id'] in chat_set,
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

        # Delete from MongoDB (with ownership enforcement)
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
        owned_ids = [j['_id'] for j in deleted_jobs]
        for jid in owned_ids:
            audit_log(jid, 'JOB_DELETED', {'deleted_by': user_id}, user_id=user_id)

        return Response({'deleted': len(owned_ids), 'job_ids': owned_ids})


# ──────────────────────────────────────────────
# Power BI Integration
# ──────────────────────────────────────────────

class ShareableView(APIView):
    """
    GET /api/share/<sharedToken> — download cleaned CSV file without authentication.
    """
    permission_classes = []  # Public endpoint

    def get(self, request, sharedToken):
        job = get_job_by_share_token(sharedToken)
        if not job or not job.get('cleaned_file_path'):
            return Response(
                {'error': 'Invalid token or file not found'},
                status=status.HTTP_404_NOT_FOUND,
            )

        cleaned_path = job['cleaned_file_path']
        if not os.path.exists(cleaned_path):
            return Response(
                {'error': 'File missing on disk'},
                status=status.HTTP_404_NOT_FOUND,
            )

        return FileResponse(
            open(cleaned_path, 'rb'),
            content_type='text/csv',
            as_attachment=True,
            filename=f"{job['_id']}-cleaned.csv",
        )


class PowerBIConfigView(APIView):
    """
    GET /api/powerbi-config/<jobId> — get .pbids file content.
    """
    permission_classes = [IsAuthenticated]

    def get(self, request, jobId):
        job = find_owned_job(jobId, request.user)
        if not job or not job.get('cleaned_file_path'):
            return Response(
                {'error': 'Cleaned file not found'},
                status=status.HTTP_404_NOT_FOUND,
            )

        # Generate or reuse share token
        share_token = job.get('share_token')
        if not share_token:
            share_token = secrets.token_urlsafe(32)
            update_job_share_token(jobId, share_token)

        # Construct protocol and host
        # Power BI typically needs an absolute URL
        protocol = 'https' if request.is_secure() else 'http'
        host = request.get_host()
        share_url = f"{protocol}://{host}/api/share/{share_token}"

        pbids_content = {
            "version": "0.1",
            "connections": [
                {
                    "details": {
                        "protocol": "web",
                        "address": {
                            "url": share_url
                        },
                        "authentication": None,
                        "query": None
                    },
                    "options": {},
                    "mode": "Import"
                }
            ]
        }

        return Response(pbids_content)


# ──────────────────────────────────────────────
# AgentOS Chat Proxy
# ──────────────────────────────────────────────

AGENT_OS_BASE_URL = os.environ.get('AGENT_OS_URL', 'http://localhost:7777')
AGENT_OS_TEAM_ID  = 'qualitylab-validation-team'


class AgentChatProxyView(APIView):
    """
    POST /api/agent-chat

    Proxies a chat message from the Angular frontend to the AgentOS team
    running on port 7777.  Keeps CORS clean and adds JWT auth protection.

    Body: { "message": str, "session_id": str, "job_id"?: str }
    Returns: { "content": str }

    When job_id is provided, the analysis results are fetched from MongoDB
    and injected as context so the agent can answer follow-up questions
    about the completed analysis.
    """
    permission_classes = [IsAuthenticated]

    def post(self, request):
        message    = request.data.get('message', '').strip()
        session_id = request.data.get('session_id', '')
        job_id     = request.data.get('job_id', '')

        if not message:
            return Response({'error': 'message is required'}, status=status.HTTP_400_BAD_REQUEST)

        # Build the message with analysis context if a job_id is provided
        enriched_message = message
        if job_id:
            try:
                analysis = get_analysis_result(job_id)
                if analysis and 'result' in analysis:
                    result = analysis['result']
                    context_parts = []
                    if result.get('schema_analysis', {}).get('report'):
                        context_parts.append(f"## Schema Analysis Report\n{result['schema_analysis']['report']}")
                    column_report = (
                        result.get('column_validator_analysis', {}).get('report')
                        or result.get('column_validator_analysis', {}).get('markdown_report')
                    )
                    if column_report:
                        context_parts.append(f"## Column Validator Analysis Report\n{column_report}")
                    if result.get('row_level_analysis', {}).get('report'):
                        context_parts.append(f"## Row-Level Analysis Report\n{result['row_level_analysis']['report']}")
                    if result.get('cross_row_analysis', {}).get('report'):
                        context_parts.append(f"## Cross-Row Analysis Report\n{result['cross_row_analysis']['report']}")
                    if context_parts:
                        context = '\n\n---\n\n'.join(context_parts)
                        enriched_message = (
                            f"[CONTEXT: The following analysis was already completed for this database. "
                            f"Use it to answer the user's question. Do NOT ask for a connection string again.]\n\n"
                            f"{context}\n\n"
                            f"---\n\n"
                            f"[USER QUESTION]: {message}"
                        )
            except Exception:
                pass  # Fall back to raw message if context fetch fails

        url = f'{AGENT_OS_BASE_URL}/teams/{AGENT_OS_TEAM_ID}/runs'

        try:
            resp = http_requests.post(
                url,
                data={
                    'message':    enriched_message,
                    'session_id': session_id,
                    'user_id':    str(request.user.id),
                    'stream':     'False',
                },
                timeout=120,   # agents can be slow
            )
            resp.raise_for_status()

            data    = resp.json()
            content = data.get('content') or data.get('output') or str(data)

            # Persist both messages to MongoDB
            if job_id:
                user_id = str(request.user.id)
                save_chat_message(job_id, user_id, 'user', message)
                save_chat_message(job_id, user_id, 'agent', content)

            return Response({'content': content})

        except http_requests.exceptions.ConnectionError:
            return Response(
                {'error': 'AgentOS server is not running. Start it with: python agents/agent_os.py'},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )
        except http_requests.exceptions.Timeout:
            return Response(
                {'error': 'Agent timed out. The database query may be too complex.'},
                status=status.HTTP_504_GATEWAY_TIMEOUT,
            )
        except Exception as exc:
            return Response(
                {'error': f'Agent error: {exc}'},
                status=status.HTTP_502_BAD_GATEWAY,
            )


class ChatHistoryView(APIView):
    """
    GET /api/chat-history/<jobId> — return all chat messages for a job.
    """
    permission_classes = [IsAuthenticated]

    def get(self, request, jobId):
        job = get_job(jobId)
        if not job:
            return Response({'error': 'Job not found'}, status=status.HTTP_404_NOT_FOUND)

        # Ownership check (admins can see all)
        if request.user.role != 'admin' and job.get('user_id') != str(request.user.id):
            return Response({'error': 'Forbidden'}, status=status.HTTP_403_FORBIDDEN)

        messages = get_chat_messages(jobId)
        # Serialize datetimes
        for m in messages:
            if 'created_at' in m:
                m['created_at'] = m['created_at'].isoformat()

        return Response({'messages': messages})

    def post(self, request, jobId):
        """Save a single chat message (used by frontend to persist analysis summary)."""
        role = request.data.get('role', '').strip()
        content = request.data.get('content', '').strip()

        if not role or not content:
            return Response({'error': 'role and content are required'}, status=status.HTTP_400_BAD_REQUEST)

        save_chat_message(jobId, str(request.user.id), role, content)
        return Response({'ok': True})


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

        return Response(fixes)


class ExecuteFixView(APIView):
    """
    POST /api/execute-fix/<job_id>/

    Body: { "queries": ["SQL1", "SQL2", ...] }

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

        # 4 — Execute all queries via isolated execution agent
        execution_log = QueryExecutorAgent().execute(
            queries=queries,
            connection_string=connection_string,
        )

        if execution_log.get('status') == 'success':
            audit_log(
                job_id,
                'FIX_EXECUTED',
                {
                    'database_id': execution_log.get('database_id'),
                    'rows_affected': execution_log.get('rows_affected'),
                    'duration_ms': execution_log.get('duration_ms'),
                    'query_count': len(queries),
                    'transaction': execution_log.get('transaction'),
                    'status_code': 200,
                },
                user_id=str(request.user.id),
            )
        else:
            audit_log(
                job_id,
                'FIX_EXECUTION_FAILED',
                {
                    'database_id': execution_log.get('database_id'),
                    'duration_ms': execution_log.get('duration_ms'),
                    'query_count': len(queries),
                    'transaction': execution_log.get('transaction'),
                    'error': execution_log.get('error'),
                    'status_code': 500,
                },
                user_id=str(request.user.id),
            )

        try:
            if bool(getattr(request.user, 'email_notifications_enabled', True)):
                user_email = getattr(request.user, 'email', '')
                notify_user_fix_executed(user_email, job_id, execution_log.get('status'))
        except Exception:
            logger.exception('Failed to send fix execution notification for job %s', job_id)

        return Response(execution_log, status=status.HTTP_200_OK)


# ──────────────────────────────────────────────
# Admin APM Monitoring
# ──────────────────────────────────────────────

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
        failed_jobs = Job.objects.filter(status__in=['failed', 'cleaning_failed']).count()
        failed_jobs_today = Job.objects.filter(
            created_at__gte=today_start,
            status__in=['failed', 'cleaning_failed'],
        ).count()

        active_jobs_qs = Job.objects.filter(
            Q(status__in=list(_ADMIN_ACTIVE_JOB_STATUSES)) | Q(status__startswith='running_')
        ).only('id', 'status', 'updated_at', 'user_id', 'file_name', 'mode').order_by('-updated_at')
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
                'file_name': entry.file_name,
                'updated_at': entry.updated_at.isoformat().replace('+00:00', 'Z') if entry.updated_at else None,
                'user_email': active_user_emails.get(entry.user_id),
            }
            for entry in active_jobs_sample
            if _is_active_job_status(entry.status)
        ]

        duration_event_types = [
            'ANALYSIS_COMPLETED',
            'DB_ANALYSIS_COMPLETED',
            'CLEANING_COMPLETED',
            'POSTGRES_CLEANING_COMPLETED',
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
                    Job.objects.filter(user_id__in=matching_user_ids).values_list('id', flat=True)
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
            row['id']: row['user_id']
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
            owner_user_id = job_owner_map.get(entry.job_id or '')
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
            1. Per-run DB `job_guidelines` entry (primary)
            2. File-based GuidelinesStore latest version (fallback)

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

        # ── 2. Fallback: file-based GuidelinesStore ────────────────────
        fallback = {}
        connection_string = job.get('connection_string')
        if connection_string:
            try:
                from agents.guidelines_store import GuidelinesStore
                store = GuidelinesStore(connection_string)
                latest = store.load_latest()
                if latest:
                    fallback = {
                        k: v for k, v in latest.items()
                        if not k.startswith('_') and isinstance(v, str)
                    }
            except Exception:
                pass  # never let the fallback crash the endpoint

        merged = {**fallback, **persisted}

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

