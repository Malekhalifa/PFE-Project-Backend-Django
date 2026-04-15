import os
import unittest
from unittest.mock import patch

import django

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
django.setup()

from services.postgres_service import analyze_postgres_database


class PostgresServiceTests(unittest.TestCase):
    @patch("services.postgres_service.orchestrator_workflow")
    def test_service_preserves_download_bundle_and_model(self, mock_orchestrator):
        expected_bundle = {
            "pipeline_run_id": "run-123",
            "database_id": "localhost_5432_demo",
            "llm_model": "gemini-1.5-pro",
            "run_timestamp": "2026-04-15T10:00:00Z",
            "summary": {"confidence_score": 90, "data_quality_score": 85},
            "reports": {
                "schema_integrity_validator": {},
                "column_validator": {},
                "row_level_validator": {},
                "cross_row_validator": {},
                "data_fix_planner": {},
            },
        }
        mock_orchestrator.return_value = {
            "pipeline_run_id": "run-123",
            "database_id": "localhost_5432_demo",
            "llm_model": "gemini-1.5-pro",
            "schema_analysis": {"report": "schema"},
            "column_validator_analysis": {"report": "column"},
            "row_level_analysis": {"report": "row"},
            "cross_row_analysis": {"report": "cross"},
            "fixes": {"fixes": []},
            "agent_reports": {},
            "final_report": {},
            "download_report_json": expected_bundle,
            "confidence_score": 90,
            "data_quality_score": 85,
            "report_file_path": "",
            "report_file_name": "",
            "report_timestamp": "2026-04-15T10:00:00Z",
            "report_database_id": "localhost_5432_demo",
        }

        result = analyze_postgres_database(
            "postgresql://user:pass@localhost:5432/demo",
            custom_rules=[],
            job_id="job-1",
            llm_model="gemini-1.5-pro",
        )

        self.assertEqual(result["llm_model"], "gemini-1.5-pro")
        self.assertEqual(result["download_report_json"], expected_bundle)

        call_kwargs = mock_orchestrator.call_args.kwargs
        self.assertEqual(call_kwargs["llm_model"], "gemini-1.5-pro")


if __name__ == "__main__":
    unittest.main()
