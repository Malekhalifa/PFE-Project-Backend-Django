import os
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import django
from rest_framework import status

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')
django.setup()

from api.views import JobDetailView


class JobDetailViewTests(unittest.TestCase):
    def _request(self, user_id: str = 'user-1', role: str = 'user'):
        return SimpleNamespace(user=SimpleNamespace(id=user_id, role=role))

    @patch('api.views.audit_log')
    @patch('api.views.update_job_celery_task_id')
    @patch('api.views.update_job_status')
    @patch('api.views.celery_app.control.revoke')
    @patch('api.views.find_owned_job')
    def test_delete_cancels_running_job_and_revokes_task(
        self,
        mock_find_owned_job,
        mock_revoke,
        mock_update_status,
        mock_update_task_id,
        mock_audit_log,
    ):
        mock_find_owned_job.return_value = {
            'status': 'running',
            'celery_task_id': 'task-123',
        }

        request = self._request()
        response = JobDetailView().delete(request, 'job-1')

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data.get('status'), 'CANCELLED')
        self.assertTrue(response.data.get('revoked'))

        mock_revoke.assert_called_once_with('task-123', terminate=True)
        mock_update_status.assert_called_once_with('job-1', 'CANCELLED')
        mock_update_task_id.assert_called_once_with('job-1', None)
        self.assertGreaterEqual(mock_audit_log.call_count, 1)

    @patch('api.views.find_owned_job')
    def test_delete_rejects_terminal_job_status(self, mock_find_owned_job):
        mock_find_owned_job.return_value = {
            'status': 'completed',
            'celery_task_id': 'task-123',
        }

        request = self._request()
        response = JobDetailView().delete(request, 'job-2')

        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)
        self.assertIn('Only running jobs can be cancelled.', response.data.get('error', ''))

    @patch('api.views.find_owned_job')
    def test_delete_is_idempotent_for_already_cancelled(self, mock_find_owned_job):
        mock_find_owned_job.return_value = {
            'status': 'CANCELLED',
            'celery_task_id': None,
        }

        request = self._request()
        response = JobDetailView().delete(request, 'job-3')

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertTrue(response.data.get('already_cancelled'))
        self.assertEqual(response.data.get('status'), 'CANCELLED')


if __name__ == '__main__':
    unittest.main()
