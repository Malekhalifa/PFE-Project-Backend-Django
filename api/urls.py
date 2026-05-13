"""
API URL routes — mounted at /api/ in config/urls.py.

PostgreSQL-only endpoints:
  - Analysis: POST /api/analyze-postgres, POST /api/test-postgres,
              GET /api/status/<jobId>, GET /api/results/<jobId>,
              GET /api/results/<jobId>/diff, GET /api/results/<jobId>/export
  - History:  GET /api/jobs, DELETE /api/jobs
  - Fixes:    GET /api/fixes/<id>/, POST /api/execute-fix/<id>/
  - Chat:     POST /api/agent-chat, GET /api/chat-history/<jobId>
  - Guidelines: GET /api/guidelines/<jobId>
  - Admin:    /api/admin/*
"""
from django.urls import path
from . import views

urlpatterns = [
    # PostgreSQL Analysis
    path('test-postgres', views.TestPostgresConnectionView.as_view(), name='test-postgres'),
    path('analyze-postgres', views.AnalyzePostgresView.as_view(), name='analyze-postgres'),
    path('status/<str:jobId>', views.StatusView.as_view(), name='status'),
    path('results/<str:jobId>', views.ResultsView.as_view(), name='results'),
    path('results/<str:jobId>/diff', views.ResultsDiffView.as_view(), name='results-diff'),
    path('results/<str:jobId>/export', views.ResultsExportView.as_view(), name='results-export'),

    # Jobs (History) — GET and DELETE on the same /api/jobs path
    path('jobs', views.JobsView.as_view(), name='jobs'),
    path('jobs/<str:job_id>', views.JobDetailView.as_view(), name='job-detail'),
    path('jobs/<str:job_id>/', views.JobDetailView.as_view(), name='job-detail-slash'),
    path('jobs/<str:job_id>/chat', views.JobChatView.as_view(), name='job-chat'),
    path('jobs/<str:job_id>/chat/', views.JobChatView.as_view(), name='job-chat-slash'),

    # AgentOS chat proxy — forwards messages to the AgentOS team on port 7777
    path('agent-chat', views.AgentChatProxyView.as_view(), name='agent-chat'),
    path('chat-history/<str:jobId>', views.ChatHistoryView.as_view(), name='chat-history'),

    # PostgreSQL Human-in-the-Loop Fix Execution
    path('fixes/<str:job_id>/', views.GetFixesView.as_view(), name='get-fixes'),
    path('execute-fix/<str:job_id>/', views.ExecuteFixView.as_view(), name='execute-fix'),

    # Admin APM Monitoring
    path('admin/stats/', views.AdminStatsView.as_view(), name='admin-stats'),
    path('admin/pipeline-performance/', views.AdminPipelinePerformanceView.as_view(), name='admin-pipeline-performance'),
    path('admin/audit-log/', views.AdminAuditLogView.as_view(), name='admin-audit-log'),
    path('admin/error-timeline/', views.AdminErrorTimelineView.as_view(), name='admin-error-timeline'),
    path('admin/test-email/', views.AdminTestEmailView.as_view(), name='admin-test-email'),
    path('admin/users/', views.AdminUsersView.as_view(), name='admin-users'),
    path('admin/users/<str:user_id>/', views.AdminUserDetailView.as_view(), name='admin-user-detail'),
    path('admin/langfuse/<str:endpoint>/', views.LangfuseProxyView.as_view(), name='admin-langfuse'),

    # AI-Generated Guidelines
    path('guidelines/<str:jobId>', views.GuidelinesView.as_view(), name='guidelines'),
]
