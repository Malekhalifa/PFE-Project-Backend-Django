import os
import uuid

import django
from django.test import TestCase
from rest_framework.test import APIRequestFactory, force_authenticate

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')
django.setup()

from api.models import AnalysisResult, Job
from api.views import ResultsExportView
from users.models import User


class ResultsExportViewTests(TestCase):
    def setUp(self):
        self.factory = APIRequestFactory()
        self.user = User(username='results-user', email='results-user@example.com')
        self.user.set_unusable_password()
        self.user.role = 'user'
        self.user.email_verified = True
        self.user.save()

        self.job = Job.objects.create(
            id=str(uuid.uuid4()),
            user_id=str(self.user.id),
            file_name='PostgreSQL Connection',
            file_path='postgres://<hidden>',
            status='completed',
            mode='agentic_postgres',
            llm_model='gemini-2.0-flash',
        )

    def test_export_falls_back_to_embedded_bundle_when_file_missing(self):
        inline_bundle = {
            'pipeline_run_id': 'run-inline-bundle',
            'database_id': 'localhost_5432_demo',
            'run_timestamp': '2026-04-15T16:00:00Z',
            'summary': {
                'confidence_score': 91,
                'data_quality_score': 87,
            },
            'reports': {
                'schema_integrity_validator': {'summary': {}, 'findings': []},
                'column_validator': {'summary': {}, 'findings': []},
                'row_level_validator': {'summary': {}, 'findings': []},
                'cross_row_validator': {'summary': {}, 'findings': []},
                'data_fix_planner': {'summary': {}, 'findings': []},
            },
        }

        result_payload = {
            'pipeline_run_id': 'run-inline-bundle',
            'database_id': 'localhost_5432_demo',
            'schema_analysis': {
                'report': 'schema markdown',
                'json_report': {'summary': {}, 'findings': []},
            },
            'column_validator_analysis': {
                'agent': 'ColumnValidatorAgent',
                'summary': {},
                'findings': [],
                'metrics': {},
                'prior_reports': [],
            },
            'row_level_analysis': {
                'report': 'row markdown',
                'json_report': {'summary': {}, 'findings': []},
            },
            'cross_row_analysis': {
                'report': 'cross markdown',
                'json_report': {'summary': {}, 'findings': []},
            },
            'final_report': {'summary': {}, 'findings': []},
            'report_file_path': '/tmp/does-not-exist-report.json',
            'report_file_name': 'report.json',
            'download_report_json': inline_bundle,
            'confidence_score': 91,
            'data_quality_score': 87,
        }

        AnalysisResult.objects.create(
            job=self.job,
            result=result_payload,
            analysis_type='postgres_agentic',
        )

        request = self.factory.get(f'/api/results/{self.job.id}/export')
        force_authenticate(request, user=self.user)

        response = ResultsExportView.as_view()(request, jobId=str(self.job.id))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data, inline_bundle)
