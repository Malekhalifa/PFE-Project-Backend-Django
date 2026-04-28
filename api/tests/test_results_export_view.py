import os
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import django
from rest_framework.test import APIRequestFactory, force_authenticate

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
django.setup()

from api.views import ResultsExportView


class ResultsExportViewTests(unittest.TestCase):
    def setUp(self):
        self.factory = APIRequestFactory()
        self.user = SimpleNamespace(
            id="user-1",
            pk="user-1",
            role="user",
            is_authenticated=True,
            email_verified=True,
        )

    @patch("api.views.get_analysis_result")
    @patch("api.views.find_owned_job")
    def test_export_falls_back_to_embedded_bundle_when_file_missing(
        self,
        mock_find_owned_job,
        mock_get_analysis_result,
    ):
        inline_bundle = {
            "pipeline_run_id": "run-inline-bundle",
            "database_id": "localhost_5432_demo",
            "run_timestamp": "2026-04-15T16:00:00Z",
            "summary": {
                "confidence_score": 91,
                "data_quality_score": 87,
            },
            "reports": {
                "schema_integrity_validator": {"summary": {}, "findings": []},
                "column_validator": {"summary": {}, "findings": []},
                "row_level_validator": {"summary": {}, "findings": []},
                "cross_row_validator": {"summary": {}, "findings": []},
                "data_fix_planner": {"summary": {}, "findings": []},
            },
        }

        result_payload = {
            "pipeline_run_id": "run-inline-bundle",
            "database_id": "localhost_5432_demo",
            "schema_analysis": {
                "report": "schema markdown",
                "json_report": {"summary": {}, "findings": []},
            },
            "column_validator_analysis": {
                "agent": "ColumnValidatorAgent",
                "summary": {},
                "findings": [],
                "metrics": {},
                "prior_reports": [],
            },
            "row_level_analysis": {
                "report": "row markdown",
                "json_report": {"summary": {}, "findings": []},
            },
            "cross_row_analysis": {
                "report": "cross markdown",
                "json_report": {"summary": {}, "findings": []},
            },
            "final_report": {"summary": {}, "findings": []},
            "report_file_path": "/tmp/does-not-exist-report.json",
            "report_file_name": "report.json",
            "download_report_json": inline_bundle,
            "confidence_score": 91,
            "data_quality_score": 87,
        }

        mock_find_owned_job.return_value = {"id": "job-1", "user_id": "user-1"}
        mock_get_analysis_result.return_value = {
            "job_id": "job-1",
            "result": result_payload,
        }

        request = self.factory.get("/api/results/job-1/export")
        force_authenticate(request, user=self.user)

        response = ResultsExportView.as_view()(request, jobId="job-1")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data, inline_bundle)


if __name__ == "__main__":
    unittest.main()
