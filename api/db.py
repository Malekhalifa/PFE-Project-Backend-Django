"""
Database access layer — PostgreSQL/Django ORM.

Drop-in replacement for the old api/mongo.py.
All public function signatures are identical so that callers only need to
change their import line from `api.mongo` to `api.db`.

Return values from job-fetching helpers are plain dicts (not model instances)
with an `_id` key to match the MongoDB document shape that all callers expect:

  job['_id']          — UUID string
  job['status']
  job['file_path']
  job.get('connection_string')
  … etc.
"""
import uuid
from datetime import datetime, timezone
from typing import Any

from django.db import transaction
from django.utils import timezone as dj_tz

from api.models import (
    Job,
    AnalysisResult,
    CleaningResult,
    ChatMessage,
    JobGuideline,
)

_GUIDELINE_KEYS = [
    'schema_integrity_validator',
    'column_validator',
    'row_level_validator',
    'cross_row_validator',
    'data_fix_planner',
]


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


def _job_to_dict(job: Job) -> dict:
    """
    Serialize a Job model instance to a plain dict that matches the shape
    callers expect from the old MongoDB document.
    """
    return {
        '_id': job.id,
        'user_id': job.user_id,
        'file_name': job.file_name,
        'file_path': job.file_path,
        'cleaned_file_path': job.cleaned_file_path,
        'connection_string': job.connection_string,
        'status': job.status,
        'mode': job.mode,
        'share_token': job.share_token,
        'cleaning_report': job.cleaning_report,
        'created_at': job.created_at,
        'updated_at': job.updated_at,
    }


def _analysis_to_dict(ar: AnalysisResult) -> dict:
    return {
        'job_id': ar.job_id,
        'result': ar.result,
        'analysis_type': ar.analysis_type,
        'created_at': ar.created_at,
        'updated_at': ar.updated_at,
    }


def _cleaning_to_dict(cr: CleaningResult) -> dict:
    return {
        'job_id': cr.job_id,
        'mode': cr.mode,
        'rules_applied': cr.rules_applied,
        'summary': cr.summary,
        'created_at': cr.created_at,
        'updated_at': cr.updated_at,
    }


# ──────────────────────────────────────────────
# Jobs
# ──────────────────────────────────────────────

def create_job(
    user_id: str,
    file_name: str,
    file_path: str,
    mode: str = 'normal',
) -> str:
    """Create a new job row. Returns the job_id (UUID string)."""
    job_id = str(uuid.uuid4())
    Job.objects.create(
        id=job_id,
        user_id=user_id,
        file_name=file_name,
        file_path=file_path,
        mode=mode,
        status='starting',
    )
    return job_id


def get_job(job_id: str) -> dict | None:
    """Find a job by ID. Returns a dict or None."""
    try:
        return _job_to_dict(Job.objects.get(id=job_id))
    except Job.DoesNotExist:
        return None


def get_jobs_for_user(user_id: str, is_admin: bool = False) -> list[dict]:
    """
    Return jobs for the authenticated user.
    Admins see all jobs; regular users see only their own.
    Sorted by created_at descending.
    """
    qs = Job.objects.all() if is_admin else Job.objects.filter(user_id=user_id)
    qs = qs.order_by('-created_at').only(
        'id', 'file_name', 'status', 'mode', 'created_at', 'user_id'
    )
    return [
        {
            '_id': j.id,
            'file_name': j.file_name,
            'status': j.status,
            'mode': j.mode,
            'created_at': j.created_at,
            'user_id': j.user_id,
        }
        for j in qs
    ]


def update_job_status(job_id: str, job_status: str) -> None:
    """Update a job's status."""
    Job.objects.filter(id=job_id).update(status=job_status, updated_at=dj_tz.now())


def update_job_cleaned_path(job_id: str, cleaned_file_path: str) -> None:
    """Set the cleaned file path on a job."""
    Job.objects.filter(id=job_id).update(
        cleaned_file_path=cleaned_file_path, updated_at=dj_tz.now()
    )


def update_job_share_token(job_id: str, share_token: str | None) -> None:
    """Set or clear the share token for a job."""
    Job.objects.filter(id=job_id).update(
        share_token=share_token, updated_at=dj_tz.now()
    )


def get_job_by_share_token(share_token: str) -> dict | None:
    """Find a job by its share token."""
    try:
        return _job_to_dict(Job.objects.get(share_token=share_token))
    except Job.DoesNotExist:
        return None


def update_job_connection_string(job_id: str, connection_string: str) -> None:
    """Persist the user's PostgreSQL DSN in the job row."""
    Job.objects.filter(id=job_id).update(
        connection_string=connection_string, updated_at=dj_tz.now()
    )


def update_job_cleaning_report(job_id: str, report: dict) -> None:
    """Persist a legacy cleaning report directly on the job row."""
    Job.objects.filter(id=job_id).update(
        cleaning_report=report, updated_at=dj_tz.now()
    )


def delete_jobs(job_ids: list[str], user_id: str = None, is_admin: bool = False) -> list[dict]:
    """
    Delete jobs by IDs, enforcing ownership.
    Returns the list of deleted job documents (as dicts).
    """
    qs = Job.objects.filter(id__in=job_ids)
    if not is_admin:
        qs = qs.filter(user_id=user_id)

    jobs = [_job_to_dict(j) for j in qs]
    owned_ids = [j['_id'] for j in jobs]

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
    payload = dict(result or {})
    # Agentic PostgreSQL runs should always expose a column validator payload.
    if 'schema_analysis' in payload and 'column_validator_analysis' not in payload:
        payload['column_validator_analysis'] = _default_column_validator_analysis()

    AnalysisResult.objects.update_or_create(
        job_id=job_id,
        defaults={
            'result': payload,
            'analysis_type': analysis_type,
        },
    )


def get_analysis_result(job_id: str) -> dict | None:
    """Find an analysis result by job_id."""
    try:
        return _analysis_to_dict(AnalysisResult.objects.get(job_id=job_id))
    except AnalysisResult.DoesNotExist:
        return None


def get_analysis_job_ids(job_ids: list[str]) -> set[str]:
    """Return set of job_ids that have analysis results."""
    return set(
        AnalysisResult.objects
        .filter(job_id__in=job_ids)
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
    CleaningResult.objects.update_or_create(
        job_id=job_id,
        defaults={
            'mode': mode,
            'rules_applied': rules_applied,
            'summary': summary,
        },
    )


def get_cleaning_result(job_id: str) -> dict | None:
    """Find a cleaning result by job_id."""
    try:
        return _cleaning_to_dict(CleaningResult.objects.get(job_id=job_id))
    except CleaningResult.DoesNotExist:
        return None


def get_cleaning_job_ids(job_ids: list[str]) -> set[str]:
    """Return set of job_ids that have cleaning results."""
    return set(
        CleaningResult.objects
        .filter(job_id__in=job_ids)
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
    ChatMessage.objects.create(
        job_id=job_id,
        user_id=user_id,
        role=role,
        content=content,
    )


def get_chat_messages(job_id: str) -> list[dict]:
    """Return all chat messages for a job, sorted by timestamp."""
    return [
        {
            'role': m.role,
            'content': m.content,
            'created_at': m.created_at,
        }
        for m in ChatMessage.objects.filter(job_id=job_id).order_by('created_at')
    ]


def get_chat_job_ids(job_ids: list[str]) -> set[str]:
    """Return set of job_ids that have chat messages."""
    return set(
        ChatMessage.objects
        .filter(job_id__in=job_ids)
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
    obj, _ = JobGuideline.objects.get_or_create(
        job_id=job_id,
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
    try:
        return JobGuideline.objects.get(job_id=job_id).guidelines or None
    except JobGuideline.DoesNotExist:
        return None
