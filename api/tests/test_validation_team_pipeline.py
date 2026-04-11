import json
import os
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import django

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
django.setup()

from agents.validation_team import _parse_column_report, orchestrator_workflow


class ValidationTeamPipelineTests(unittest.TestCase):
    def test_parse_column_report_accepts_fenced_json(self):
        raw = """```json
{
  "agent": "ColumnValidatorAgent",
  "database_id": "db_1",
  "run_timestamp": "2026-04-11T12:00:00Z",
  "findings": [],
  "summary": {
    "total_items_analyzed": 0,
    "items_passed": 0,
    "items_flagged": 0,
    "critical_issues": 0,
    "high_issues": 0,
    "medium_issues": 0,
    "low_issues": 0,
    "overall_quality_score": 100,
    "overall_confidence": 1.0,
    "flagged_tables": []
  },
  "metrics": {
    "total_columns_analyzed": 0,
    "duplicate_columns_found": 0,
    "constant_columns_found": 0,
    "high_null_ratio_columns": 0,
    "low_cardinality_anomalies": 0,
    "high_cardinality_anomalies": 0,
    "semantic_mismatches": 0,
    "domain_violations": 0,
    "distribution_anomalies": 0,
    "avg_column_quality_score": 100,
    "avg_column_confidence": 1.0
  },
  "prior_reports": []
}
```"""
        parsed, text = _parse_column_report(raw)

        self.assertEqual(parsed["agent"], "ColumnValidatorAgent")
        self.assertEqual(parsed["database_id"], "db_1")
        self.assertIn("summary", parsed)
        self.assertTrue(text.startswith("{"))

    def test_parse_column_report_falls_back_on_invalid_json(self):
        parsed, text = _parse_column_report("not-json")

        self.assertEqual(parsed["agent"], "ColumnValidatorAgent")
        self.assertEqual(parsed["error"], "invalid_column_validator_json")
        self.assertIn("not-json", parsed["raw"])
        self.assertIn("invalid_column_validator_json", text)

    @patch("services.report_service.save_final_pipeline_report")
    @patch("agents.validation_team.DatabaseGuidelineGeneratorAgent")
    @patch("agents.validation_team.Agent")
    @patch("agents.validation_team.postgres_tool")
    def test_pipeline_builds_chained_reports_and_metadata(
        self,
        mock_postgres_tool,
        mock_agent_cls,
        mock_generator_cls,
        mock_save_report,
    ):
        generator = mock_generator_cls.return_value
        generator.generate.side_effect = [
            "schema-guidelines",
            "column-guidelines",
            "row-guidelines",
            "cross-guidelines",
            "fix-guidelines",
        ]

        mock_save_report.return_value = {
            "report_file_path": "/tmp/report_localhost_5432_db.json",
            "report_file_name": "report_localhost_5432_db.json",
            "report_timestamp": "2026-04-11T12:05:00Z",
            "report_database_id": "localhost_5432_db",
        }

        schema_payload = {
            "findings": [
                {
                    "table": "users",
                    "issues": [
                        {
                            "type": "missing_primary_key",
                            "severity": "high",
                            "description": "users has no PK",
                        }
                    ],
                    "confidence": 0.88,
                }
            ],
            "metrics": {
                "total_tables": 2,
                "total_columns": 7,
                "tables_with_no_primary_key": 1,
                "missing_foreign_keys": 0,
                "constraint_violations": 0,
                "type_mismatches": 0,
                "nullable_violations": 0,
            },
        }

        column_payload = {
            "tables": [
                {
                    "table": "users",
                    "columns": [
                        {
                            "column": "email",
                            "quality_score": 70,
                            "confidence": 0.82,
                            "status": "invalid",
                            "issues": [
                                {
                                    "type": "semantic_mismatch",
                                    "severity": "medium",
                                    "description": "email contains invalid domains",
                                }
                            ],
                            "metrics": {
                                "null_ratio": 0.1,
                                "distinct_ratio": 0.9,
                                "dominant_value_ratio": 0.15,
                                "inferred_type_distribution": {"email": 0.8, "text": 0.2},
                            },
                        }
                    ],
                }
            ],
            "summary": {
                "total_columns_analyzed": 3,
            },
        }

        row_payload = {
            "findings": [
                {
                    "table": "users",
                    "row_id": "42",
                    "issues": [
                        {
                            "type": "format_violation_email",
                            "severity": "medium",
                            "description": "invalid email format",
                        }
                    ],
                    "confidence": 0.75,
                }
            ],
            "metrics": {
                "total_rows_analyzed": 100,
            },
        }

        cross_payload = {
            "findings": [
                {
                    "table": "events",
                    "issues": [
                        {
                            "type": "temporal_anomaly_spike",
                            "severity": "high",
                            "description": "unusual spike detected",
                        }
                    ],
                    "confidence": 0.79,
                }
            ],
            "metrics": {
                "total_row_pairs_analyzed": 60,
            },
        }

        fix_payload = {
            "findings": [
                {
                    "id": "fix_001",
                    "table": "users",
                    "target_table": "users_clean",
                    "problem": "invalid email formats",
                    "fix": "normalize email values",
                    "severity": "high",
                    "confidence": 0.84,
                    "estimated_rows_affected": 20,
                    "queries": [
                        "CREATE TABLE IF NOT EXISTS users_clean (LIKE users INCLUDING ALL);",
                        "INSERT INTO users_clean SELECT * FROM users ON CONFLICT DO NOTHING;",
                        "UPDATE users_clean SET email = lower(email);",
                    ],
                    "explanation": "Normalize to lowercase for consistency",
                },
                {
                    "id": "fix_002",
                    "table": "events",
                    "target_table": "events_clean",
                    "problem": "temporal anomaly outliers",
                    "fix": "cap abnormal spikes",
                    "severity": "medium",
                    "confidence": 0.8,
                    "estimated_rows_affected": 5,
                    "queries": [
                        "CREATE TABLE IF NOT EXISTS events_clean (LIKE events INCLUDING ALL);",
                        "INSERT INTO events_clean SELECT * FROM events ON CONFLICT DO NOTHING;",
                        "UPDATE events_clean SET amount = amount WHERE amount IS NOT NULL;",
                    ],
                    "explanation": "Preserve trends while reducing spike noise",
                },
            ]
        }

        responses = [
            SimpleNamespace(content=json.dumps(schema_payload)),
            SimpleNamespace(content=json.dumps(column_payload)),
            SimpleNamespace(content=json.dumps(row_payload)),
            SimpleNamespace(content=json.dumps(cross_payload)),
            SimpleNamespace(content=json.dumps(fix_payload)),
        ]

        agent_instances = []
        for response in responses:
            instance = MagicMock()
            instance.run.return_value = response
            agent_instances.append(instance)
        mock_agent_cls.side_effect = agent_instances

        result = orchestrator_workflow(
            connection_string="postgresql://user:pass@localhost:5432/db",
            domain_rules="",
        )

        call_order = [
            call.kwargs["target_agent"] for call in generator.generate.call_args_list
        ]
        self.assertEqual(
            call_order,
            [
                "schema_integrity_validator",
                "column_validator",
                "row_level_validator",
                "cross_row_validator",
                "data_fix_planner",
            ],
        )

        reports = result["agent_reports"]
        schema_report = reports["schema_integrity_validator"]
        column_report = reports["column_validator"]
        row_report = reports["row_level_validator"]
        cross_report = reports["cross_row_validator"]
        final_report = reports["data_fix_planner"]

        self.assertEqual(len(schema_report["prior_reports"]), 0)
        self.assertEqual(len(column_report["prior_reports"]), 1)
        self.assertEqual(len(row_report["prior_reports"]), 2)
        self.assertEqual(len(cross_report["prior_reports"]), 3)
        self.assertEqual(len(final_report["prior_reports"]), 4)

        pipeline_run_id = schema_report["pipeline_run_id"]
        self.assertEqual(column_report["pipeline_run_id"], pipeline_run_id)
        self.assertEqual(row_report["pipeline_run_id"], pipeline_run_id)
        self.assertEqual(cross_report["pipeline_run_id"], pipeline_run_id)
        self.assertEqual(final_report["pipeline_run_id"], pipeline_run_id)

        expected_total_issues = sum(
            reports[key]["summary"]["items_flagged"]
            for key in [
                "schema_integrity_validator",
                "column_validator",
                "row_level_validator",
                "cross_row_validator",
            ]
        )
        self.assertEqual(
            final_report["metrics"]["total_issues_received"],
            expected_total_issues,
        )

        self.assertIn("report_file_path", result)
        self.assertEqual(result["report_file_name"], "report_localhost_5432_db.json")
        self.assertIn("fixes", result)
        self.assertEqual(len(result["fixes"]["fixes"]), 2)

    @patch("services.report_service.save_final_pipeline_report")
    @patch("agents.validation_team.DatabaseGuidelineGeneratorAgent")
    @patch("agents.validation_team.Agent")
    @patch("agents.validation_team.postgres_tool")
    def test_pipeline_marks_partial_when_output_invalid(
        self,
        mock_postgres_tool,
        mock_agent_cls,
        mock_generator_cls,
        mock_save_report,
    ):
        generator = mock_generator_cls.return_value
        generator.generate.side_effect = [
            "schema-guidelines",
            "column-guidelines",
            "row-guidelines",
            "cross-guidelines",
            "fix-guidelines",
        ]

        mock_save_report.return_value = {
            "report_file_path": "/tmp/report_localhost_5432_db.json",
            "report_file_name": "report_localhost_5432_db.json",
            "report_timestamp": "2026-04-11T12:05:00Z",
            "report_database_id": "localhost_5432_db",
        }

        row_payload = {
            "findings": [],
            "metrics": {"total_rows_analyzed": 1},
        }
        cross_payload = {
            "findings": [],
            "metrics": {"total_row_pairs_analyzed": 1},
        }
        fix_payload = {
            "findings": [],
        }

        responses = [
            SimpleNamespace(content="this-is-not-json"),
            SimpleNamespace(content="this-is-not-json-either"),
            SimpleNamespace(content=json.dumps(row_payload)),
            SimpleNamespace(content=json.dumps(cross_payload)),
            SimpleNamespace(content=json.dumps(fix_payload)),
        ]

        agent_instances = []
        for response in responses:
            instance = MagicMock()
            instance.run.return_value = response
            agent_instances.append(instance)
        mock_agent_cls.side_effect = agent_instances

        result = orchestrator_workflow(
            connection_string="postgresql://user:pass@localhost:5432/db",
            domain_rules="",
        )

        reports = result["agent_reports"]
        self.assertEqual(reports["schema_integrity_validator"].get("status"), "partial")
        self.assertEqual(reports["column_validator"].get("status"), "partial")
        # Pipeline still progresses and emits final report.
        self.assertIn("data_fix_planner", reports)
        self.assertIn("report_file_path", result)


if __name__ == "__main__":
    unittest.main()
