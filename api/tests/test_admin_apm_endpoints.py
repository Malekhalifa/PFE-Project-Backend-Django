import os
import uuid
from datetime import timedelta
from unittest.mock import patch

import django
from django.test import TestCase, override_settings
from django.utils import timezone as dj_tz
from rest_framework.test import APIRequestFactory, force_authenticate

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')
django.setup()

from api.models import AuditLog, Job
from api.views import (
    AdminAuditLogView,
    AdminErrorTimelineView,
    AdminPipelinePerformanceView,
    AdminStatsView,
    AdminTestEmailView,
)
from users.models import User


class AdminApmEndpointTests(TestCase):
    def setUp(self):
        self.factory = APIRequestFactory()
        self.staff_user = self._build_user(
            username='admin_user',
            email='admin@example.com',
            role='admin',
            is_staff=True,
        )
        self.regular_user = self._build_user(
            username='regular_user',
            email='user@example.com',
            role='user',
            is_staff=False,
        )

    def _build_user(self, *, username: str, email: str, role: str, is_staff: bool) -> User:
        user = User(username=username, email=email)
        user.set_unusable_password()
        user.save()
        user.role = role
        user.is_staff = is_staff
        user.email_verified = True
        user.save(update_fields=['role', 'is_staff', 'email_verified'])
        return user

    def _create_job(
        self,
        *,
        user: User,
        status: str,
        mode: str = 'normal',
        created_at=None,
        updated_at=None,
    ) -> Job:
        job = Job.objects.create(
            id=str(uuid.uuid4()),
            user_id=str(user.id),
            file_name='dataset.csv',
            file_path='/tmp/dataset.csv',
            status=status,
            mode=mode,
        )

        updates = {}
        if created_at is not None:
            updates['created_at'] = created_at
        if updated_at is not None:
            updates['updated_at'] = updated_at
        if updates:
            Job.objects.filter(id=job.id).update(**updates)
            job.refresh_from_db()

        return job

    def _create_audit(self, *, job_id: str, event_type: str, payload: dict, created_at=None) -> AuditLog:
        row = AuditLog.objects.create(job_id=job_id, event_type=event_type, payload=payload)
        if created_at is not None:
            AuditLog.objects.filter(id=row.id).update(created_at=created_at)
            row.refresh_from_db()
        return row

    def _auth_request(self, *, method: str, path: str, user: User, data=None):
        method = method.lower().strip()
        if method == 'get':
            request = self.factory.get(path, data=data or {})
        elif method == 'post':
            request = self.factory.post(path, data=data or {}, format='json')
        else:
            raise ValueError(f'Unsupported method: {method}')

        force_authenticate(request, user=user)
        return request

    def test_admin_endpoints_require_staff_user(self):
        endpoints = [
            ('get', '/api/admin/stats/', AdminStatsView.as_view(), None),
            ('get', '/api/admin/pipeline-performance/', AdminPipelinePerformanceView.as_view(), None),
            ('get', '/api/admin/audit-log/', AdminAuditLogView.as_view(), None),
            ('get', '/api/admin/error-timeline/', AdminErrorTimelineView.as_view(), None),
            ('post', '/api/admin/test-email/', AdminTestEmailView.as_view(), {}),
        ]

        for method, path, view, payload in endpoints:
            with self.subTest(endpoint=path):
                request = self._auth_request(
                    method=method,
                    path=path,
                    user=self.regular_user,
                    data=payload,
                )
                response = view(request)

                self.assertEqual(response.status_code, 403)
                self.assertEqual(response.data.get('code'), 'admin_required')

    def test_admin_stats_returns_expected_shape_and_metrics(self):
        now = dj_tz.now()
        old_timestamp = now - timedelta(days=3)

        active_job = self._create_job(
            user=self.regular_user,
            status='running_schema',
            updated_at=now - timedelta(minutes=3),
        )
        self._create_job(user=self.regular_user, status='failed')
        self._create_job(user=self.regular_user, status='completed', created_at=old_timestamp)

        self._create_audit(
            job_id=active_job.id,
            event_type='ANALYSIS_COMPLETED',
            payload={'duration_ms': 120, 'status_code': 200},
        )
        self._create_audit(
            job_id=active_job.id,
            event_type='DB_ANALYSIS_COMPLETED',
            payload={'duration_ms': 400, 'status_code': 200},
        )
        self._create_audit(
            job_id=active_job.id,
            event_type='FIX_EXECUTED',
            payload={'duration_ms': 80, 'status_code': 200},
        )
        self._create_audit(
            job_id=active_job.id,
            event_type='ANALYSIS_FAILED',
            payload={'status_code': 500, 'error': 'boom'},
        )

        request = self._auth_request(
            method='get',
            path='/api/admin/stats/',
            user=self.staff_user,
        )
        response = AdminStatsView.as_view()(request)

        self.assertEqual(response.status_code, 200)
        data = response.data

        self.assertEqual(data.get('total_users'), 2)
        self.assertEqual(data.get('total_jobs'), 3)
        self.assertEqual(data.get('jobs_today'), 2)
        self.assertEqual(data.get('active_jobs'), 1)
        self.assertEqual(data.get('failed_jobs'), 1)
        self.assertEqual(data.get('failed_jobs_today'), 1)
        self.assertEqual(data.get('fixes_applied_today'), 1)

        self.assertGreater(data.get('average_response_time_ms', 0), 0)
        self.assertIsNotNone(data.get('slowest_endpoint'))
        self.assertIsNotNone(data.get('last_updated'))
        self.assertIn(data.get('health_status'), {'healthy', 'warning', 'critical'})

        active_jobs = data.get('active_jobs_details') or []
        self.assertEqual(len(active_jobs), 1)
        self.assertEqual(active_jobs[0].get('job_id'), active_job.id)
        self.assertEqual(active_jobs[0].get('user_email'), self.regular_user.email)

    def test_pipeline_performance_returns_measured_metrics_when_step_events_exist(self):
        job = self._create_job(user=self.regular_user, status='completed', mode='agentic_postgres')

        self._create_audit(
            job_id=job.id,
            event_type='PIPELINE_STEP_COMPLETED',
            payload={'step': 'schema_validation', 'duration_ms': 150, 'status_code': 200},
        )
        self._create_audit(
            job_id=job.id,
            event_type='PIPELINE_STEP_COMPLETED',
            payload={'step': 'schema_validation', 'duration_ms': 250, 'status_code': 200},
        )
        self._create_audit(
            job_id=job.id,
            event_type='PIPELINE_STEP_FAILED',
            payload={'step': 'schema_validation', 'duration_ms': 300, 'status_code': 500},
        )

        request = self._auth_request(
            method='get',
            path='/api/admin/pipeline-performance/',
            user=self.staff_user,
            data={'days': 7},
        )
        response = AdminPipelinePerformanceView.as_view()(request)

        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.data.get('uses_estimation'))

        step_rows = response.data.get('steps') or []
        schema_metric = next((row for row in step_rows if row.get('step') == 'schema_validation'), None)
        self.assertIsNotNone(schema_metric)
        self.assertEqual(schema_metric.get('source'), 'measured')
        self.assertEqual(schema_metric.get('sample_count'), 3)
        self.assertAlmostEqual(float(schema_metric.get('success_rate')), 66.67, places=2)
        self.assertAlmostEqual(float(schema_metric.get('average_duration_ms')), 233.33, places=2)

    def test_pipeline_performance_falls_back_to_estimated_mode_without_step_events(self):
        job = self._create_job(user=self.regular_user, status='completed', mode='agentic_postgres')

        self._create_audit(
            job_id=job.id,
            event_type='DB_ANALYSIS_COMPLETED',
            payload={'duration_ms': 1000, 'status_code': 200},
        )
        self._create_audit(
            job_id=job.id,
            event_type='DB_ANALYSIS_FAILED',
            payload={'status_code': 500, 'error': 'network'},
        )

        request = self._auth_request(
            method='get',
            path='/api/admin/pipeline-performance/',
            user=self.staff_user,
            data={'days': 7},
        )
        response = AdminPipelinePerformanceView.as_view()(request)

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.data.get('uses_estimation'))

        step_rows = response.data.get('steps') or []
        schema_metric = next((row for row in step_rows if row.get('step') == 'schema_validation'), None)
        self.assertIsNotNone(schema_metric)
        self.assertEqual(schema_metric.get('source'), 'estimated')
        self.assertEqual(schema_metric.get('sample_count'), 1)
        self.assertAlmostEqual(float(schema_metric.get('average_duration_ms')), 100.0, places=2)
        self.assertAlmostEqual(float(schema_metric.get('success_rate')), 50.0, places=2)

    def test_audit_log_supports_filter_combinations(self):
        now = dj_tz.now()
        stale_time = now - timedelta(days=5)

        matching_job = self._create_job(user=self.regular_user, status='completed')
        other_job = self._create_job(user=self.staff_user, status='completed')

        self._create_audit(
            job_id=matching_job.id,
            event_type='FIX_EXECUTION_FAILED',
            payload={'status_code': 500, 'error': 'constraint'},
            created_at=now,
        )
        self._create_audit(
            job_id=matching_job.id,
            event_type='FIX_EXECUTED',
            payload={'status_code': 200},
            created_at=stale_time,
        )
        self._create_audit(
            job_id=other_job.id,
            event_type='FIX_EXECUTION_FAILED',
            payload={'status_code': 500, 'error': 'timeout'},
            created_at=now,
        )

        request = self._auth_request(
            method='get',
            path='/api/admin/audit-log/',
            user=self.staff_user,
            data={
                'event': 'FIX',
                'job_id': matching_job.id,
                'user_email': self.regular_user.email,
                'date_from': (now - timedelta(days=1)).date().isoformat(),
                'date_to': (now + timedelta(days=1)).date().isoformat(),
                'page': 1,
                'limit': 25,
            },
        )
        response = AdminAuditLogView.as_view()(request)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data.get('total'), 1)
        self.assertEqual(len(response.data.get('items') or []), 1)

        entry = response.data['items'][0]
        self.assertEqual(entry.get('event'), 'FIX_EXECUTION_FAILED')
        self.assertEqual(entry.get('job_id'), matching_job.id)
        self.assertEqual(entry.get('user_email'), self.regular_user.email)
        self.assertEqual(entry.get('status'), 'error')
        self.assertEqual(entry.get('status_code'), 500)
        self.assertIn('status_code', entry.get('detail', ''))

    def test_audit_log_pagination_returns_expected_page_metadata(self):
        job = self._create_job(user=self.regular_user, status='completed')
        for idx in range(3):
            self._create_audit(
                job_id=job.id,
                event_type=f'PIPELINE_EVENT_{idx}',
                payload={'status_code': 200},
            )

        request = self._auth_request(
            method='get',
            path='/api/admin/audit-log/',
            user=self.staff_user,
            data={'page': 2, 'limit': 2},
        )
        response = AdminAuditLogView.as_view()(request)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data.get('page'), 2)
        self.assertEqual(response.data.get('limit'), 2)
        self.assertEqual(response.data.get('total'), 3)
        self.assertEqual(response.data.get('total_pages'), 2)
        self.assertEqual(len(response.data.get('items') or []), 1)

    def test_error_timeline_counts_success_and_error_events(self):
        now = dj_tz.now()
        job = self._create_job(user=self.regular_user, status='completed')

        self._create_audit(
            job_id=job.id,
            event_type='ANALYSIS_COMPLETED',
            payload={'status_code': 200},
            created_at=now - timedelta(hours=1),
        )
        self._create_audit(
            job_id=job.id,
            event_type='ANALYSIS_FAILED',
            payload={'status_code': 500},
            created_at=now - timedelta(hours=1),
        )
        self._create_audit(
            job_id=job.id,
            event_type='DB_ANALYSIS_COMPLETED',
            payload={},
            created_at=now - timedelta(hours=2),
        )
        self._create_audit(
            job_id=job.id,
            event_type='DB_ANALYSIS_FAILED',
            payload={},
            created_at=now - timedelta(hours=2),
        )

        request = self._auth_request(
            method='get',
            path='/api/admin/error-timeline/',
            user=self.staff_user,
            data={'hours': 24},
        )
        response = AdminErrorTimelineView.as_view()(request)

        self.assertEqual(response.status_code, 200)
        timeline = response.data.get('timeline') or []
        self.assertEqual(len(timeline), 24)

        total_errors = sum(item.get('error_count', 0) for item in timeline)
        total_successes = sum(item.get('success_count', 0) for item in timeline)
        self.assertEqual(total_errors, 2)
        self.assertEqual(total_successes, 2)

    @override_settings(ADMIN_EMAIL='ops@example.com', DEFAULT_FROM_EMAIL='noreply@example.com')
    @patch('api.views.audit_log')
    @patch('api.views.send_mail', return_value=1)
    def test_admin_test_email_sends_diagnostic_email(self, mock_send_mail, mock_audit_log):
        request = self._auth_request(
            method='post',
            path='/api/admin/test-email/',
            user=self.staff_user,
            data={},
        )
        response = AdminTestEmailView.as_view()(request)

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.data.get('ok'))
        self.assertEqual(response.data.get('recipient'), 'ops@example.com')
        self.assertEqual(response.data.get('sent_count'), 1)
        mock_send_mail.assert_called_once()
        mock_audit_log.assert_called_once()
        self.assertEqual(mock_audit_log.call_args[0][1], 'ADMIN_TEST_EMAIL_SENT')

    @override_settings(ADMIN_EMAIL='')
    def test_admin_test_email_returns_400_when_admin_email_missing(self):
        request = self._auth_request(
            method='post',
            path='/api/admin/test-email/',
            user=self.staff_user,
            data={},
        )
        response = AdminTestEmailView.as_view()(request)

        self.assertEqual(response.status_code, 400)
        self.assertFalse(response.data.get('ok'))
        self.assertEqual(response.data.get('code'), 'admin_email_missing')
