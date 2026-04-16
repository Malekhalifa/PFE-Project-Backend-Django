import os
import unittest

import django
from django.core.exceptions import FieldDoesNotExist
from django.db import models

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')
django.setup()

from api.models import Job


class JobModelSchemaTests(unittest.TestCase):
    def test_job_primary_key_is_uuid(self):
        job_id = Job._meta.get_field('id')
        self.assertIsInstance(job_id, models.UUIDField)
        self.assertTrue(job_id.primary_key)

    def test_job_owner_is_foreign_key(self):
        owner = Job._meta.get_field('user')
        self.assertIsInstance(owner, models.ForeignKey)
        self.assertEqual(owner.db_column, 'user_id')

    def test_job_has_no_legacy_cleaning_report_field(self):
        with self.assertRaises(FieldDoesNotExist):
            Job._meta.get_field('cleaning_report')


if __name__ == '__main__':
    unittest.main()
