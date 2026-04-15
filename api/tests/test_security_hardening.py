import os
import uuid

import django
from django.conf import settings
from django.test import TestCase

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')
django.setup()

from api.connection_string_crypto import ENCRYPTED_PREFIX
from api.db import get_job, update_job_connection_string
from api.models import AuditLog, Job, audit_log
from api.views import (
    AnalyzePostgresUserRateThrottle,
    AnalyzePostgresView,
    ExecuteFixUserRateThrottle,
    ExecuteFixView,
)
from users.models import User


class SecurityHardeningTests(TestCase):
    def setUp(self):
        self.user = User(username='security-user', email='security@example.com')
        self.user.set_unusable_password()
        self.user.save()
        self.job = Job.objects.create(
            id=str(uuid.uuid4()),
            user_id=str(self.user.id),
            file_name='dataset.csv',
            file_path='/tmp/dataset.csv',
            status='starting',
            mode='normal',
        )

    def test_job_connection_string_is_encrypted_at_rest(self):
        plain = 'postgresql://user:pass@localhost:5432/qualitylab'

        update_job_connection_string(self.job.id, plain)

        stored = Job.objects.get(id=self.job.id).connection_string
        self.assertIsInstance(stored, str)
        self.assertTrue(stored.startswith(ENCRYPTED_PREFIX))
        self.assertNotEqual(stored, plain)

        hydrated = get_job(self.job.id)
        self.assertEqual(hydrated.get('connection_string'), plain)

    def test_audit_log_records_explicit_user_id(self):
        audit_log(self.job.id, 'SECURITY_TEST_EXPLICIT', {'ok': True}, user_id=str(self.user.id))

        row = AuditLog.objects.get(event_type='SECURITY_TEST_EXPLICIT')
        self.assertEqual(str(row.user_id), str(self.user.id))

    def test_audit_log_backfills_user_id_from_job(self):
        audit_log(self.job.id, 'SECURITY_TEST_IMPLICIT', {'ok': True})

        row = AuditLog.objects.get(event_type='SECURITY_TEST_IMPLICIT')
        self.assertEqual(str(row.user_id), str(self.user.id))

    def test_expensive_views_define_scoped_user_throttles(self):
        self.assertEqual(AnalyzePostgresView.throttle_classes, [AnalyzePostgresUserRateThrottle])
        self.assertEqual(ExecuteFixView.throttle_classes, [ExecuteFixUserRateThrottle])
        self.assertEqual(AnalyzePostgresUserRateThrottle.scope, 'analyze_postgres')
        self.assertEqual(ExecuteFixUserRateThrottle.scope, 'execute_fix')

        rates = settings.REST_FRAMEWORK.get('DEFAULT_THROTTLE_RATES', {})
        self.assertIn('analyze_postgres', rates)
        self.assertIn('execute_fix', rates)
