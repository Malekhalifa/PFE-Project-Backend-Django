"""
Database access layer — PostgreSQL/Django ORM.

This layer now uses UUID-native identifiers and ORM-oriented field names.
"""
import logging
import uuid
from datetime import datetime, timezone
from typing import Any

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Q
from django.utils import timezone as dj_tz

from api.models import (
    Job,
    AnalysisResult,
    CleaningResult,
    ChatMessage,
    JobGuideline,
)
from api.connection_string_crypto import (
    decrypt_connection_string,
    encrypt_connection_string,
)


logger = logging.getLogger(__name__)

_GUIDELINE_KEYS = [
    'schema_integrity_validator',
    'column_validator',
    'row_level_validator',
    'cross_row_validator',
    'data_fix_planner',
]
_DEFAULT_LLM_MODEL = 'gemini-2.0-flash'


def _default_column_validator_analysis() -> dict[str, Any]:
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
        'message': 'Column validator output was not persisted for this run.',
    }


# ──────────────────────────────────────────────
# Internal helpers
# ──────────────────────────────────────────────

def _now() -> datetime:
    return datetime.now(timezone.utc)


def _normalize_uuid(value: str | uuid.UUID | None) -> str | None:
    if value is None:
        return None
    try:
        return str(uuid.UUID(str(value).strip()))
    except (ValueError, ValidationError, TypeError, AttributeError):
        return None


def _serialize_job(job: Job) -> dict:
    """
    Serialize a Job instance to a plain dict with UUID-native semantics.
    """
    try:
        decrypted_connection_string = decrypt_connection_string(job.connection_string)
    except ValueError:
        logger.exception('Failed to decrypt connection string for job %s', job.id)
        decrypted_connection_string = None

    return {
        'id': str(job.id),
        'user_id': str(job.user_id) if job.user_id else None,
        'file_name': job.file_name,
        'file_path': job.file_path,
        'cleaned_file_path': job.cleaned_file_path,
        'connection_string': decrypted_connection_string,
        'status': job.status,
        'celery_task_id': job.celery_task_id,
        'mode': job.mode,
        'llm_model': job.llm_model,
        'created_at': job.created_at,
        'updated_at': job.updated_at,
    }


def _analysis_to_dict(ar: AnalysisResult) -> dict:
    return {
        'job_id': str(ar.job_id),
        'result': ar.result,
        'analysis_type': ar.analysis_type,
        'created_at': ar.created_at,
        'updated_at': ar.updated_at,
    }


def _cleaning_to_dict(cr: CleaningResult) -> dict:
    return {
        'job_id': str(cr.job_id),
        'mode': cr.mode,
        'rules_applied': cr.rules_applied,
        'summary': cr.summary,
        'execution_log': cr.execution_log,
        'created_at': cr.created_at,
        'updated_at': cr.updated_at,
    }


def _legacy_cleaning_summary(report: dict | None) -> dict:
    if not isinstance(report, dict):
        return {}
    summary = report.get('summary')
    if isinstance(summary, dict):
        return summary
    tables = report.get('tables')
    return {
        'status': report.get('status'),
        'tables_processed': len(tables) if isinstance(tables, list) else 0,
    }


# ──────────────────────────────────────────────
# Jobs
# ──────────────────────────────────────────────

def create_job(
    user_id: str,
    file_name: str,
    file_path: str,
    mode: str = 'normal',
    llm_model: str = _DEFAULT_LLM_MODEL,
) -> str:
    """Create a new job row. Returns the job_id (UUID string)."""
    normalized_user_id = _normalize_uuid(user_id)
    if not normalized_user_id:
        raise ValueError('Invalid user_id supplied to create_job')

    job = Job.objects.create(
        user_id=normalized_user_id,
        file_name=file_name,
        file_path=file_path,
        mode=mode,
        llm_model=(llm_model or _DEFAULT_LLM_MODEL).strip() or _DEFAULT_LLM_MODEL,
        status='starting',
    )
    return str(job.id)


def get_job(job_id: str) -> dict | None:
    """Find a job by ID. Returns a dict or None."""
    normalized_job_id = _normalize_uuid(job_id)
    if not normalized_job_id:
        return None

    try:
        return _serialize_job(Job.objects.get(id=normalized_job_id))
    except Job.DoesNotExist:
        return None


def get_jobs_for_user(user_id: str, is_admin: bool = False) -> list[dict]:
    """
    Return jobs for the authenticated user.
    Admins see all jobs; regular users see only their own.
    Sorted by created_at descending.
    """
    normalized_user_id = _normalize_uuid(user_id)
    if not is_admin and not normalized_user_id:
        return []

    qs = Job.objects.all() if is_admin else Job.objects.filter(user_id=normalized_user_id)
    qs = qs.order_by('-created_at').only(
        'id', 'file_name', 'status', 'mode', 'llm_model', 'created_at', 'user_id'
    )
    return [
        {
            'id': str(j.id),
            'file_name': j.file_name,
            'status': j.status,
            'mode': j.mode,
            'llm_model': j.llm_model,
            'created_at': j.created_at,
            'user_id': str(j.user_id),
        }
        for j in qs
    ]


def update_job_status(job_id: str, job_status: str) -> None:
    """Update a job's status."""
    normalized_job_id = _normalize_uuid(job_id)
    if not normalized_job_id:
        return
    status_value = str(job_status or '').strip()
    if not status_value:
        return

    now = dj_tz.now()
    if status_value == 'CANCELLED':
        Job.objects.filter(id=normalized_job_id).update(status='CANCELLED', updated_at=now)
        return

    Job.objects.filter(id=normalized_job_id).exclude(status='CANCELLED').update(
        status=status_value,
        updated_at=now,
    )


def update_job_celery_task_id(job_id: str, task_id: str | None) -> None:
    """Persist the latest Celery task identifier for a job."""
    normalized_job_id = _normalize_uuid(job_id)
    if not normalized_job_id:
        return

    normalized_task_id = (task_id or '').strip() or None
    Job.objects.filter(id=normalized_job_id).update(
        celery_task_id=normalized_task_id,
        updated_at=dj_tz.now(),
    )


def update_job_cleaned_path(job_id: str, cleaned_file_path: str) -> None:
    """Set the cleaned file path on a job."""
    normalized_job_id = _normalize_uuid(job_id)
    if not normalized_job_id:
        return
    Job.objects.filter(id=normalized_job_id).update(
        cleaned_file_path=cleaned_file_path, updated_at=dj_tz.now()
    )


def update_job_connection_string(job_id: str, connection_string: str) -> None:
    """Persist the user's PostgreSQL DSN in the job row."""
    normalized_job_id = _normalize_uuid(job_id)
    if not normalized_job_id:
        return
    encrypted_connection_string = encrypt_connection_string(connection_string)
    Job.objects.filter(id=normalized_job_id).update(
        connection_string=encrypted_connection_string,
        updated_at=dj_tz.now(),
    )


def update_job_cleaning_report(job_id: str, report: dict) -> None:
    """Persist a legacy cleaning execution log under CleaningResult.execution_log."""
    normalized_job_id = _normalize_uuid(job_id)
    if not normalized_job_id:
        return

    defaults = {
        'mode': 'postgres_plan_legacy',
        'rules_applied': [],
        'summary': _legacy_cleaning_summary(report),
        'execution_log': report or {},
    }
    obj, created = CleaningResult.objects.get_or_create(
        job_id=normalized_job_id,
        defaults=defaults,
    )

    if created:
        return

    update_fields: list[str] = ['execution_log', 'updated_at']
    obj.execution_log = report or {}

    if not obj.summary:
        obj.summary = _legacy_cleaning_summary(report)
        update_fields.append('summary')

    if not obj.mode:
        obj.mode = 'postgres_plan_legacy'
        update_fields.append('mode')

    if obj.rules_applied is None:
        obj.rules_applied = []
        update_fields.append('rules_applied')

    obj.save(update_fields=update_fields)


def delete_jobs(job_ids: list[str], user_id: str = None, is_admin: bool = False) -> list[dict]:
    """
    Delete jobs by IDs, enforcing ownership.
    Returns the list of deleted job documents (as dicts).
    """
    normalized_job_ids = [jid for raw in (job_ids or []) if (jid := _normalize_uuid(raw))]
    if not normalized_job_ids:
        return []

    qs = Job.objects.filter(id__in=normalized_job_ids)
    if not is_admin:
        normalized_user_id = _normalize_uuid(user_id)
        if not normalized_user_id:
            return []
        qs = qs.filter(user_id=normalized_user_id)

    jobs = [_serialize_job(j) for j in qs]
    owned_ids = [j['id'] for j in jobs]

    if owned_ids:
        with transaction.atomic():
            Job.objects.filter(id__in=owned_ids).delete()
            # Cascade deletes handle related rows (AnalysisResult, etc.)

    return jobs


# ──────────────────────────────────────────────
# Analysis results
# ──────────────────────────────────────────────

def save_analysis_result(
    job_id: str,
    result: dict[str, Any],
    analysis_type: str = 'in-memory',
) -> None:
    """Upsert an analysis result for a job."""
    normalized_job_id = _normalize_uuid(job_id)
    if not normalized_job_id:
        return

    payload = dict(result or {})
    # Agentic PostgreSQL runs should always expose a column validator payload.
    if 'schema_analysis' in payload and 'column_validator_analysis' not in payload:
        payload['column_validator_analysis'] = _default_column_validator_analysis()

    obj, _ = AnalysisResult.objects.update_or_create(
        job_id=normalized_job_id,
        defaults={
            'result': payload,
            'analysis_type': analysis_type,
        },
    )

    database_id = _extract_database_id(payload)
    if not database_id:
        return

    user_id = (
        Job.objects
        .filter(id=normalized_job_id)
        .values_list('user_id', flat=True)
        .first()
    )
    if not user_id:
        return

    previous = _find_previous_analysis_result(
        current_job_id=normalized_job_id,
        user_id=str(user_id),
        database_id=database_id,
    )
    previous_id = previous.id if previous else None
    if obj.previous_run_id == previous_id:
        return

    obj.previous_run = previous
    obj.save(update_fields=['previous_run', 'updated_at'])


def _extract_database_id(payload: dict[str, Any]) -> str | None:
    top_level = (payload.get('database_id') or '').strip()
    if top_level:
        return top_level

    report_level = (payload.get('report_database_id') or '').strip()
    if report_level:
        return report_level

    final_report = payload.get('final_report')
    if isinstance(final_report, dict):
        nested = (final_report.get('database_id') or '').strip()
        if nested:
            return nested

    return None


def _find_previous_analysis_result(
    *,
    current_job_id: str,
    user_id: str,
    database_id: str,
) -> AnalysisResult | None:
    return (
        AnalysisResult.objects
        .filter(job__user_id=user_id)
        .exclude(job_id=current_job_id)
        .filter(
            Q(result__database_id=database_id)
            | Q(result__report_database_id=database_id)
            | Q(result__final_report__database_id=database_id)
        )
        .select_related('job')
        .order_by('-created_at')
        .first()
    )


def get_analysis_result(job_id: str) -> dict | None:
    """Find an analysis result by job_id."""
    normalized_job_id = _normalize_uuid(job_id)
    if not normalized_job_id:
        return None

    try:
        return _analysis_to_dict(AnalysisResult.objects.get(job_id=normalized_job_id))
    except AnalysisResult.DoesNotExist:
        return None


def get_analysis_job_ids(job_ids: list[str]) -> set[str]:
    """Return set of job_ids that have analysis results."""
    normalized_job_ids = [jid for raw in (job_ids or []) if (jid := _normalize_uuid(raw))]
    if not normalized_job_ids:
        return set()

    return set(
        str(jid)
        for jid in AnalysisResult.objects
        .filter(job_id__in=normalized_job_ids)
        .values_list('job_id', flat=True)
    )


# ──────────────────────────────────────────────
# Cleaning results
# ──────────────────────────────────────────────

def save_cleaning_result(
    job_id: str,
    mode: str,
    rules_applied: list[dict],
    summary: dict,
) -> None:
    """Upsert a cleaning result for a job."""
    normalized_job_id = _normalize_uuid(job_id)
    if not normalized_job_id:
        return

    CleaningResult.objects.update_or_create(
        job_id=normalized_job_id,
        defaults={
            'mode': mode,
            'rules_applied': rules_applied,
            'summary': summary,
        },
    )


def get_cleaning_result(job_id: str) -> dict | None:
    """Find a cleaning result by job_id."""
    normalized_job_id = _normalize_uuid(job_id)
    if not normalized_job_id:
        return None

    try:
        return _cleaning_to_dict(CleaningResult.objects.get(job_id=normalized_job_id))
    except CleaningResult.DoesNotExist:
        return None


def get_cleaning_job_ids(job_ids: list[str]) -> set[str]:
    """Return set of job_ids that have cleaning results."""
    normalized_job_ids = [jid for raw in (job_ids or []) if (jid := _normalize_uuid(raw))]
    if not normalized_job_ids:
        return set()

    return set(
        str(jid)
        for jid in CleaningResult.objects
        .filter(job_id__in=normalized_job_ids)
        .values_list('job_id', flat=True)
    )


# ──────────────────────────────────────────────
# Chat messages
# ──────────────────────────────────────────────

def save_chat_message(
    job_id: str,
    user_id: str,
    role: str,
    content: str,
) -> None:
    """Append a single chat message (user or agent) for a job."""
    normalized_job_id = _normalize_uuid(job_id)
    if not normalized_job_id:
        return

    ChatMessage.objects.create(
        job_id=normalized_job_id,
        user_id=user_id,
        role=role,
        content=content,
    )


def get_chat_messages(job_id: str) -> list[dict]:
    """Return all chat messages for a job, sorted by timestamp."""
    normalized_job_id = _normalize_uuid(job_id)
    if not normalized_job_id:
        return []

    return [
        {
            'role': m.role,
            'content': m.content,
            'created_at': m.created_at,
        }
        for m in ChatMessage.objects.filter(job_id=normalized_job_id).order_by('created_at')
    ]


def get_chat_job_ids(job_ids: list[str]) -> set[str]:
    """Return set of job_ids that have chat messages."""
    normalized_job_ids = [jid for raw in (job_ids or []) if (jid := _normalize_uuid(raw))]
    if not normalized_job_ids:
        return set()

    return set(
        str(jid)
        for jid in ChatMessage.objects
        .filter(job_id__in=normalized_job_ids)
        .values_list('job_id', flat=True)
        .distinct()
    )


# ──────────────────────────────────────────────
# Job guidelines
# ──────────────────────────────────────────────

def save_job_guideline(job_id: str, agent_key: str, guideline_text: str) -> None:
    """
    Upsert one agent's guideline text for a job.

        agent_key is one of:
            'schema_integrity_validator' | 'column_validator' |
            'row_level_validator'        | 'cross_row_validator' |
            'data_fix_planner'

    Guidelines accumulate as the pipeline progresses.
    """
    normalized_job_id = _normalize_uuid(job_id)
    if not normalized_job_id:
        return

    obj, _ = JobGuideline.objects.get_or_create(
        job_id=normalized_job_id,
        defaults={'guidelines': {}},
    )
    base = dict.fromkeys(_GUIDELINE_KEYS, '')
    # Merge-update the specific agent key
    obj.guidelines = {
        **base,
        **(obj.guidelines or {}),
        agent_key: guideline_text,
    }
    obj.save(update_fields=['guidelines', 'updated_at'])


def get_job_guidelines(job_id: str) -> dict[str, str] | None:
    """
    Return the full guidelines map for a job, or None if not found.
    Shape: { 'schema_integrity_validator': '...', ... }
    """
    normalized_job_id = _normalize_uuid(job_id)
    if not normalized_job_id:
        return None

    try:
        return JobGuideline.objects.get(job_id=normalized_job_id).guidelines or None
    except JobGuideline.DoesNotExist:
        return None
