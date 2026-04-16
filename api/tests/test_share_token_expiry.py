import os
import uuid
from datetime import timedelta

import django
from django.test import TestCase
from django.utils import timezone as dj_tz
from rest_framework.test import APIRequestFactory, force_authenticate

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')
django.setup()

from api.db import get_job_by_share_token, update_job_share_token
from api.models import Job
from api.views import PowerBIConfigView
from users.models import User


class ShareTokenExpiryTests(TestCase):
    def setUp(self):
        self.factory = APIRequestFactory()
        self.user = User(username='share-user', email='share@example.com')
        self.user.set_unusable_password()
        self.user.save()
        self.job = Job.objects.create(
            id=str(uuid.uuid4()),
            user_id=str(self.user.id),
            file_name='dataset.csv',
            file_path='/tmp/dataset.csv',
            cleaned_file_path='/tmp/dataset-clean.csv',
            status='completed',
            mode='normal',
        )

    def test_get_job_by_share_token_rejects_expired_token(self):
        update_job_share_token(
            str(self.job.id),
            'expired-token',
            expires_at=dj_tz.now() - timedelta(minutes=1),
        )

        hydrated = get_job_by_share_token('expired-token')
        self.assertIsNone(hydrated)

    def test_get_job_by_share_token_accepts_active_token(self):
        update_job_share_token(
            str(self.job.id),
            'active-token',
            expires_at=dj_tz.now() + timedelta(hours=1),
        )

        hydrated = get_job_by_share_token('active-token')
        self.assertIsNotNone(hydrated)
        self.assertEqual(str(hydrated.get('id')), str(self.job.id))

    def test_powerbi_config_rotates_expired_share_token(self):
        update_job_share_token(
            str(self.job.id),
            'old-token',
            expires_at=dj_tz.now() - timedelta(minutes=1),
        )

        request = self.factory.get(f'/api/powerbi-config/{self.job.id}')
        force_authenticate(request, user=self.user)
        response = PowerBIConfigView.as_view()(request, jobId=str(self.job.id))

        self.assertEqual(response.status_code, 200)
        share_url = response.data['connections'][0]['details']['address']['url']
        self.assertIn('/api/share/', share_url)
        self.assertNotIn('old-token', share_url)

        self.job.refresh_from_db()
        self.assertTrue(bool(self.job.share_token))
        self.assertNotEqual(self.job.share_token, 'old-token')
        self.assertIsNotNone(self.job.share_token_expires_at)
        self.assertGreater(self.job.share_token_expires_at, dj_tz.now())
