"""
PostgreSQL models for the api app.

Replaces the legacy document-store layer entirely. Each model corresponds to
one persisted runtime entity:

  Job              ← jobs
  AnalysisResult   ← analysis_results
  ChatMessage      ← chat_messages
  AuditLog         ← audit_log
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
    connection_string = models.TextField(null=True, blank=True)

    # Job lifecycle
    status = models.CharField(max_length=64, default='starting', db_index=True)
    celery_task_id = models.CharField(max_length=128, null=True, blank=True, db_index=True)
    mode = models.CharField(max_length=64, default='agentic_postgres')
    llm_model = models.CharField(max_length=128, default='gemini-2.5-flash')

    # Audit timestamps
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'jobs'
        ordering = ['-created_at']

    def __str__(self):
        return f'Job({self.id}) [{self.status}]'


# ──────────────────────────────────────────────
# Analysis results
# ──────────────────────────────────────────────

class AnalysisResult(models.Model):
    """Stores the full AI/statistical analysis output for a job."""
    job = models.OneToOneField(
        Job, on_delete=models.CASCADE, related_name='analysis_result',
        db_column='job_id',
    )
    previous_run = models.ForeignKey(
        'self',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='next_runs',
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


# ──────────────────────────────────────────────
# Learning patterns
# ──────────────────────────────────────────────

class LearningPattern(models.Model):
    """
    Stores a user's accept/reject decision for a (rule_family, entity) pair.
    The reducer reads these to downweight findings matching rejected patterns.
    """
    DECISION_ACCEPT = 'accept'
    DECISION_REJECT = 'reject'
    DECISION_CHOICES = [
        (DECISION_ACCEPT, 'Accept'),
        (DECISION_REJECT, 'Reject'),
    ]

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='learning_patterns',
        db_column='user_id',
    )
    rule_family = models.CharField(max_length=128)
    entity = models.CharField(max_length=255)
    decision = models.CharField(max_length=16, choices=DECISION_CHOICES)
    frequency = models.PositiveIntegerField(default=1)
    last_seen = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'learning_patterns'
        unique_together = [('user', 'rule_family', 'entity')]

    def __str__(self):
        return f'LearningPattern({self.user_id}, {self.rule_family}, {self.entity}, {self.decision})'


# ──────────────────────────────────────────────
# Learned rules (per-database regression guards)
# ──────────────────────────────────────────────

class LearnedRule(models.Model):
    """
    Stores a structural/logical violation found on a previous run as a
    regression guard for future runs.

    Keyed on (database_id, table_name, rule_text) — one row per unique
    business constraint discovered per database.

    consecutive_clean_runs: incremented each run where the rule finds 0
    violations. Pruned automatically at >= 3 clean runs (fix held).
    """
    database_id = models.CharField(max_length=255, db_index=True)
    table_name = models.CharField(max_length=255)
    rule_text = models.TextField()
    rule_type = models.CharField(max_length=128)
    consecutive_clean_runs = models.PositiveIntegerField(default=0)
    last_violation_run_id = models.CharField(max_length=64, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'learned_rules'
        unique_together = [('database_id', 'table_name', 'rule_text')]
        indexes = [
            models.Index(fields=['database_id', 'consecutive_clean_runs']),
        ]

    def __str__(self):
        return f'LearnedRule({self.database_id}, {self.table_name})'


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
