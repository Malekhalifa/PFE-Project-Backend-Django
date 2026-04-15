import hashlib
import hmac
import json
import os
from unittest.mock import Mock, patch

import django
from django.test import TestCase

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')
django.setup()

from api.models import Job
from services.notification_service import notify_job_webhook_event
from users.models import User


class WebhookNotificationTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username='webhook-user',
            email='webhook-user@example.com',
            password='not-used',
            role='user',
            email_verified=True,
            notifications_enabled=True,
            webhook_url='https://ci.example.com/callback',
        )
        self.user.set_webhook_secret('super-secret')
        self.user.save(update_fields=['webhook_secret_encrypted', 'webhook_url'])

        self.job = Job.objects.create(
            user=self.user,
            file_name='PostgreSQL Connection',
            file_path='postgres://<hidden>',
            mode='agentic_postgres',
            status='running',
        )

    @patch('services.notification_service.time.time', return_value=1710000000)
    @patch('services.notification_service.http_requests.post')
    def test_notify_job_webhook_event_sends_hmac_signed_payload(self, mock_post, _mock_time):
        mock_response = Mock()
        mock_response.status_code = 202
        mock_post.return_value = mock_response

        result = notify_job_webhook_event(
            job_id=str(self.job.id),
            event_type='job.completed',
            status='completed',
            payload={'duration_ms': 3210},
        )

        self.assertTrue(result.get('ok'))
        self.assertEqual(result.get('status_code'), 202)

        call_kwargs = mock_post.call_args.kwargs
        headers = call_kwargs['headers']
        payload_bytes = call_kwargs['data']

        expected_body = {
            'event': 'job.completed',
            'job_id': str(self.job.id),
            'status': 'completed',
            'timestamp': 1710000000,
            'payload': {'duration_ms': 3210},
        }
        expected_bytes = json.dumps(expected_body, separators=(',', ':'), sort_keys=True).encode('utf-8')
        self.assertEqual(payload_bytes, expected_bytes)

        expected_signature = hmac.new(
            key='super-secret'.encode('utf-8'),
            msg=b'1710000000.' + expected_bytes,
            digestmod=hashlib.sha256,
        ).hexdigest()

        self.assertEqual(headers.get('X-QualityLab-Event'), 'job.completed')
        self.assertEqual(headers.get('X-QualityLab-Timestamp'), '1710000000')
        self.assertEqual(headers.get('X-QualityLab-Signature'), f'sha256={expected_signature}')

    @patch('services.notification_service.http_requests.post')
    def test_notify_job_webhook_event_skips_when_secret_missing(self, mock_post):
        self.user.clear_webhook_secret()
        self.user.save(update_fields=['webhook_secret_encrypted'])

        result = notify_job_webhook_event(
            job_id=str(self.job.id),
            event_type='job.completed',
            status='completed',
        )

        self.assertFalse(result.get('ok'))
        self.assertTrue(result.get('skipped'))
        self.assertEqual(result.get('reason'), 'webhook_secret_missing')
        mock_post.assert_not_called()
