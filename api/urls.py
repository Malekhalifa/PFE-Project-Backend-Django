"""
API URL routes — mounted at /api/ in config/urls.py.

Mirrors every endpoint from the Node.js backend:
  - Upload:   POST /api/upload
  - Analysis: POST /api/analyze/<jobId>, GET /api/status/<jobId>,
              GET /api/results/<jobId>, GET /api/results/<jobId>/export,
              GET /api/raw/<jobId>, GET /api/raw_with_outliers/<jobId>
  - Cleaning: POST /api/clean/<jobId>, GET /api/cleaned/<jobId>,
              GET /api/cleaning-result/<jobId>, GET /api/cleaning-result/<jobId>/export
  - History:  GET /api/jobs, DELETE /api/jobs
"""
from django.urls import path
from . import views

urlpatterns = [
    # Upload
    path('upload', views.UploadView.as_view(), name='upload'),

    # Analysis
    path('analyze/<str:jobId>', views.AnalyzeView.as_view(), name='analyze'),
    path('test-postgres', views.TestPostgresConnectionView.as_view(), name='test-postgres'),
    path('analyze-postgres', views.AnalyzePostgresView.as_view(), name='analyze-postgres'),
    path('status/<str:jobId>', views.StatusView.as_view(), name='status'),
    path('results/<str:jobId>', views.ResultsView.as_view(), name='results'),
    path('results/<str:jobId>/export', views.ResultsExportView.as_view(), name='results-export'),
    path('raw/<str:jobId>', views.RawDataView.as_view(), name='raw'),
    path('raw_with_outliers/<str:jobId>', views.RawWithOutliersView.as_view(), name='raw-with-outliers'),

    # Cleaning
    path('clean/<str:jobId>', views.CleanView.as_view(), name='clean'),
    path('cleaned/<str:jobId>', views.CleanedFileView.as_view(), name='cleaned'),
    path('cleaning-result/<str:jobId>', views.CleaningResultView.as_view(), name='cleaning-result'),
    path('cleaning-result/<str:jobId>/export', views.CleaningResultExportView.as_view(), name='cleaning-result-export'),

    # Jobs (History) — GET and DELETE on the same /api/jobs path
    path('jobs', views.JobsView.as_view(), name='jobs'),

    # Power BI Integration
    path('share/<str:sharedToken>', views.ShareableView.as_view(), name='share'),
    path('powerbi-config/<str:jobId>', views.PowerBIConfigView.as_view(), name='powerbi-config'),

    # AgentOS chat proxy — forwards messages to the AgentOS team on port 7777
    path('agent-chat', views.AgentChatProxyView.as_view(), name='agent-chat'),
    path('chat-history/<str:jobId>', views.ChatHistoryView.as_view(), name='chat-history'),
]
