from django.contrib import admin
from .models import AuditLog


@admin.register(AuditLog)
class AuditLogAdmin(admin.ModelAdmin):
    list_display = ('event_type', 'job_id', 'created_at')
    list_filter = ('event_type',)
    search_fields = ('job_id', 'event_type')
    readonly_fields = ('job_id', 'event_type', 'payload', 'created_at')
    ordering = ('-created_at',)
