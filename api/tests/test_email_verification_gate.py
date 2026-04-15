import os
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import django

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')
django.setup()

from api.views import (  # noqa: E402
    AnalyzePostgresView,
    TestPostgresConnectionView,
    _require_verified_email,
)


class EmailVerificationGateTests(unittest.TestCase):
    def test_gate_blocks_unverified_users(self):
        request = SimpleNamespace(user=SimpleNamespace(email_verified=False))

        response = _require_verified_email(request)

        self.assertIsNotNone(response)
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.data.get('code'), 'email_not_verified')

    def test_gate_allows_verified_users(self):
        request = SimpleNamespace(user=SimpleNamespace(email_verified=True))

        response = _require_verified_email(request)

        self.assertIsNone(response)

    @patch('services.postgres_connection.parse_postgres_dsn_info')
    @patch('api.views.test_postgres_connection')
    @patch('api.views.looks_like_postgres_dsn', return_value=True)
    def test_test_postgres_allows_unverified_users(
        self,
        _mock_looks_like_dsn,
        _mock_test_connection,
        mock_parse_dsn_info,
    ):
        mock_parse_dsn_info.return_value = SimpleNamespace(
            database='demo_db',
            host='localhost',
            port=5432,
        )

        request = SimpleNamespace(
            user=SimpleNamespace(email_verified=False),
            data={'connection_string': 'postgresql://demo:demo@localhost:5432/demo_db'},
        )

        response = TestPostgresConnectionView().post(request)

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.data.get('success'))
        self.assertEqual(response.data.get('database'), 'demo_db')

    def test_analyze_postgres_still_blocks_unverified_users(self):
        request = SimpleNamespace(
            user=SimpleNamespace(email_verified=False),
            data={'connection_string': 'postgresql://demo:demo@localhost:5432/demo_db'},
        )

        response = AnalyzePostgresView().post(request)

        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.data.get('code'), 'email_not_verified')

    @patch('api.views.audit_log')
    @patch('api.views.analyze_postgres_task')
    @patch('api.views.update_job_connection_string')
    @patch('api.views.create_job', return_value='job-custom-model')
    @patch('api.views.test_postgres_connection')
    @patch('api.views.looks_like_postgres_dsn', return_value=True)
    def test_analyze_postgres_accepts_custom_llm_model(
        self,
        _mock_looks_like_dsn,
        _mock_test_connection,
        mock_create_job,
        _mock_update_connection,
        mock_analyze_task,
        _mock_audit_log,
    ):
        request = SimpleNamespace(
            user=SimpleNamespace(id='user-1', email_verified=True),
            data={
                'connection_string': 'postgresql://demo:demo@localhost:5432/demo_db',
                'llm_model': 'gemini-1.5-pro',
            },
        )

        response = AnalyzePostgresView().post(request)

        self.assertEqual(response.status_code, 200)
        create_kwargs = mock_create_job.call_args.kwargs
        self.assertEqual(create_kwargs.get('llm_model'), 'gemini-1.5-pro')

        delay_kwargs = mock_analyze_task.delay.call_args.kwargs
        self.assertEqual(delay_kwargs.get('llm_model'), 'gemini-1.5-pro')

    @patch('api.views.audit_log')
    @patch('api.views.analyze_postgres_task')
    @patch('api.views.update_job_connection_string')
    @patch('api.views.create_job', return_value='job-default-model')
    @patch('api.views.test_postgres_connection')
    @patch('api.views.looks_like_postgres_dsn', return_value=True)
    def test_analyze_postgres_defaults_llm_model_when_missing(
        self,
        _mock_looks_like_dsn,
        _mock_test_connection,
        mock_create_job,
        _mock_update_connection,
        mock_analyze_task,
        _mock_audit_log,
    ):
        request = SimpleNamespace(
            user=SimpleNamespace(id='user-2', email_verified=True),
            data={
                'connection_string': 'postgresql://demo:demo@localhost:5432/demo_db',
            },
        )

        response = AnalyzePostgresView().post(request)

        self.assertEqual(response.status_code, 200)
        create_kwargs = mock_create_job.call_args.kwargs
        self.assertEqual(create_kwargs.get('llm_model'), 'gemini-2.0-flash')

        delay_kwargs = mock_analyze_task.delay.call_args.kwargs
        self.assertEqual(delay_kwargs.get('llm_model'), 'gemini-2.0-flash')


if __name__ == '__main__':
    unittest.main()
