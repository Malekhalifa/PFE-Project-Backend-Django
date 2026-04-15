import os

import django
from django.test import TestCase
from rest_framework.test import APIRequestFactory, force_authenticate

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')
django.setup()

from api.models import AnalysisResult, Job
from api.views import ResultsDiffView
from users.models import User


class ResultsDiffViewTests(TestCase):
    def setUp(self):
        self.factory = APIRequestFactory()
        self.user = User.objects.create_user(
            username='diff-user',
            email='diff-user@example.com',
            password='not-used',
            role='user',
            email_verified=True,
        )

        self.previous_job = Job.objects.create(
            user=self.user,
            file_name='PostgreSQL Connection',
            file_path='postgres://<hidden>',
            status='completed',
            mode='agentic_postgres',
        )
        self.current_job = Job.objects.create(
            user=self.user,
            file_name='PostgreSQL Connection',
            file_path='postgres://<hidden>',
            status='completed',
            mode='agentic_postgres',
        )
        self.override_job = Job.objects.create(
            user=self.user,
            file_name='PostgreSQL Connection',
            file_path='postgres://<hidden>',
            status='completed',
            mode='agentic_postgres',
        )

        self.previous_result = AnalysisResult.objects.create(
            job=self.previous_job,
            result={
                'pipeline_run_id': 'run-1',
                'database_id': 'localhost_5432_demo',
                'data_quality_score': 82,
                'column_validator_analysis': {
                    'summary': {
                        'overall_quality_score': 82,
                        'flagged_tables': ['users', 'orders'],
                    }
                },
            },
            analysis_type='postgres_agentic',
        )
        self.current_result = AnalysisResult.objects.create(
            job=self.current_job,
            previous_run=self.previous_result,
            result={
                'pipeline_run_id': 'run-2',
                'database_id': 'localhost_5432_demo',
                'data_quality_score': 91,
                'column_validator_analysis': {
                    'summary': {
                        'overall_quality_score': 91,
                        'flagged_tables': ['users'],
                    }
                },
            },
            analysis_type='postgres_agentic',
        )
        AnalysisResult.objects.create(
            job=self.override_job,
            result={
                'pipeline_run_id': 'run-0',
                'database_id': 'localhost_5432_demo',
                'data_quality_score': 70,
                'column_validator_analysis': {
                    'summary': {
                        'overall_quality_score': 70,
                        'flagged_tables': ['users', 'orders', 'logs'],
                    }
                },
            },
            analysis_type='postgres_agentic',
        )

    def test_diff_uses_previous_run_by_default(self):
        request = self.factory.get(f'/api/results/{self.current_job.id}/diff')
        force_authenticate(request, user=self.user)

        response = ResultsDiffView.as_view()(request, jobId=str(self.current_job.id))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data['current_run']['job_id'], str(self.current_job.id))
        self.assertEqual(response.data['previous_run']['job_id'], str(self.previous_job.id))
        self.assertEqual(response.data['diff']['quality_score']['delta'], 9.0)
        self.assertEqual(response.data['diff']['quality_score']['trend'], 'improved')
        self.assertEqual(response.data['diff']['flagged_tables']['resolved'], ['orders'])
        self.assertEqual(response.data['diff']['flagged_tables']['unchanged'], ['users'])

    def test_diff_compare_to_override(self):
        request = self.factory.get(
            f'/api/results/{self.current_job.id}/diff',
            {'compare_to': str(self.override_job.id)},
        )
        force_authenticate(request, user=self.user)

        response = ResultsDiffView.as_view()(request, jobId=str(self.current_job.id))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data['previous_run']['job_id'], str(self.override_job.id))
        self.assertEqual(response.data['diff']['quality_score']['delta'], 21.0)
        self.assertEqual(response.data['diff']['flagged_tables']['resolved'], ['logs', 'orders'])

