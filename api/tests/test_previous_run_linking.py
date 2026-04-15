import os

import django
from django.test import TestCase

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')
django.setup()

from api.db import save_analysis_result
from api.models import AnalysisResult, Job
from users.models import User


class PreviousRunLinkingTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username='link-user',
            email='link-user@example.com',
            password='not-used',
            role='user',
            email_verified=True,
        )

        self.first_job = Job.objects.create(
            user=self.user,
            file_name='PostgreSQL Connection',
            file_path='postgres://<hidden>',
            status='completed',
            mode='agentic_postgres',
        )
        self.second_job = Job.objects.create(
            user=self.user,
            file_name='PostgreSQL Connection',
            file_path='postgres://<hidden>',
            status='completed',
            mode='agentic_postgres',
        )

    def test_save_analysis_result_links_previous_run_for_same_database(self):
        save_analysis_result(
            job_id=str(self.first_job.id),
            analysis_type='postgres_agentic',
            result={
                'pipeline_run_id': 'run-1',
                'database_id': 'localhost_5432_qualitylab',
                'data_quality_score': 81,
            },
        )
        save_analysis_result(
            job_id=str(self.second_job.id),
            analysis_type='postgres_agentic',
            result={
                'pipeline_run_id': 'run-2',
                'database_id': 'localhost_5432_qualitylab',
                'data_quality_score': 88,
            },
        )

        second_result = AnalysisResult.objects.select_related('previous_run__job').get(job=self.second_job)

        self.assertIsNotNone(second_result.previous_run)
        self.assertEqual(second_result.previous_run.job_id, self.first_job.id)

    def test_save_analysis_result_keeps_previous_run_empty_for_first_entry(self):
        save_analysis_result(
            job_id=str(self.first_job.id),
            analysis_type='postgres_agentic',
            result={
                'pipeline_run_id': 'run-1',
                'database_id': 'localhost_5432_qualitylab',
            },
        )

        first_result = AnalysisResult.objects.get(job=self.first_job)
        self.assertIsNone(first_result.previous_run)
