"""
PostgreSQL models for the api app.

Replaces MongoDB collections entirely.  Each model corresponds to one of
the former MongoDB collections:

  Job              ← jobs
  AnalysisResult   ← analysis_results
  CleaningResult   ← cleaning_results
  ChatMessage      ← chat_messages
  JobGuideline     ← job_guidelines
  AuditLog         ← audit_log  (was already here, unchanged)
"""
import uuid
from django.core.exceptions import ValidationError
from django.db import models
from django.conf import settings


# ──────────────────────────────────────────────
# Jobs
# ──────────────────────────────────────────────

class Job(models.Model):
    """
    Tracks a single analysis/cleaning job.
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    # Ownership
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='jobs',
        db_column='user_id',
    )

    # File / connection metadata
    file_name = models.CharField(max_length=500, blank=True, default='')
    file_path = models.TextField(blank=True, default='')
    cleaned_file_path = models.TextField(null=True, blank=True)
    connection_string = models.TextField(null=True, blank=True)

    # Job lifecycle
    status = models.CharField(max_length=64, default='starting', db_index=True)
    mode = models.CharField(max_length=64, default='normal')
    llm_model = models.CharField(max_length=128, default='gemini-2.0-flash')

    # Power BI share
    share_token = models.CharField(max_length=128, null=True, blank=True, db_index=True)

    # Audit (matches MongoDB created_at / updated_at)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'jobs'
        ordering = ['-created_at']

    def __str__(self):
        return f'Job({self.id}) — {self.file_name} [{self.status}]'


# ──────────────────────────────────────────────
# Analysis results
# ──────────────────────────────────────────────

class AnalysisResult(models.Model):
    """Stores the full AI/statistical analysis output for a job."""
    job = models.OneToOneField(
        Job, on_delete=models.CASCADE, related_name='analysis_result',
        db_column='job_id',
    )
    result = models.JSONField(default=dict)
    analysis_type = models.CharField(max_length=64, default='in-memory')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'analysis_results'

    def __str__(self):
        return f'AnalysisResult({self.job_id})'


# ──────────────────────────────────────────────
# Cleaning results
# ──────────────────────────────────────────────

class CleaningResult(models.Model):
    """Stores the cleaning summary and applied rules for a job."""
    job = models.OneToOneField(
        Job, on_delete=models.CASCADE, related_name='cleaning_result',
        db_column='job_id',
    )
    mode = models.CharField(max_length=64, default='auto')
    rules_applied = models.JSONField(default=list)
    summary = models.JSONField(default=dict)
    execution_log = models.JSONField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'cleaning_results'

    def __str__(self):
        return f'CleaningResult({self.job_id})'


# ──────────────────────────────────────────────
# Chat messages
# ──────────────────────────────────────────────

class ChatMessage(models.Model):
    """Append-only log of user/agent chat messages for a job."""
    job = models.ForeignKey(
        Job, on_delete=models.CASCADE, related_name='chat_messages',
        db_column='job_id', db_index=True,
    )
    user_id = models.CharField(max_length=36)
    role = models.CharField(max_length=16)   # 'user' | 'agent'
    content = models.TextField()
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        db_table = 'chat_messages'
        ordering = ['created_at']

    def __str__(self):
        return f'ChatMessage({self.job_id}, {self.role})'


# ──────────────────────────────────────────────
# Job guidelines
# ──────────────────────────────────────────────

class JobGuideline(models.Model):
    """
    Per-run AI-generated guideline text for each pipeline phase.

        guidelines shape:
            {
                "schema_integrity_validator": "...",
                "column_validator":           "...",
                "cross_row_validator":        "...",
                "row_level_validator":        "...",
                "data_fix_planner":           "..."
            }
    """
    job = models.OneToOneField(
        Job, on_delete=models.CASCADE, related_name='guideline',
        db_column='job_id',
    )
    guidelines = models.JSONField(default=dict)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'job_guidelines'

    def __str__(self):
        return f'JobGuideline({self.job_id})'


# ──────────────────────────────────────────────
# Audit log
# ──────────────────────────────────────────────

class AuditLog(models.Model):
    """
    Audit log entry — records events like JOB_CREATED, FILE_UPLOADED,
    ANALYSIS_STARTED, ANALYSIS_COMPLETED, CLEANING_STARTED, etc.
    """
    job_id = models.CharField(max_length=255, blank=True, null=True, db_index=True)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='audit_logs',
        db_column='user_id',
    )
    event_type = models.CharField(max_length=100)
    payload = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'audit_log'
        ordering = ['-created_at']

    def __str__(self):
        return f'{self.event_type} — {self.job_id}'


def audit_log(
    job_id: str,
    event_type: str,
    payload: dict | None = None,
    user_id: str | None = None,
) -> None:
    """
    Convenience function to create an audit log entry.
    Matches the Node.js auditLog(jobId, eventType, payload) function,
    while optionally recording who triggered the event.
    """
    resolved_user_id = str(user_id).strip() if user_id else None
    if not resolved_user_id and job_id:
        try:
            normalized_job_id = uuid.UUID(str(job_id).strip())
        except (ValueError, ValidationError, TypeError):
            normalized_job_id = None

        if normalized_job_id is not None:
            resolved_user_id = (
                Job.objects
                .filter(id=normalized_job_id)
                .values_list('user_id', flat=True)
                .first()
            )

    AuditLog.objects.create(
        job_id=str(job_id).strip() if job_id else None,
        user_id=resolved_user_id,
        event_type=event_type,
        payload=payload or {},
    )
