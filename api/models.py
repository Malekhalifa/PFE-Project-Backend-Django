"""
PostgreSQL models for the api app.

AuditLog — mirrors the current Node.js/PostgreSQL audit_log table.
"""
from django.db import models


class AuditLog(models.Model):
    """
    Audit log entry — records events like JOB_CREATED, FILE_UPLOADED,
    ANALYSIS_STARTED, ANALYSIS_COMPLETED, CLEANING_STARTED, etc.
    """
    job_id = models.CharField(max_length=255, blank=True, null=True, db_index=True)
    event_type = models.CharField(max_length=100)
    payload = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'audit_log'
        ordering = ['-created_at']

    def __str__(self):
        return f'{self.event_type} — {self.job_id}'


def audit_log(job_id: str, event_type: str, payload: dict | None = None) -> None:
    """
    Convenience function to create an audit log entry.
    Matches the Node.js auditLog(jobId, eventType, payload) function.
    """
    AuditLog.objects.create(
        job_id=job_id,
        event_type=event_type,
        payload=payload or {},
    )
