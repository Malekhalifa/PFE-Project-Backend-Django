import os
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import django

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
django.setup()

from agents.reporting import report_to_markdown
from agents.schemas import Finding, FixCard, FixPlan, ReducedFindings, ValidationReport
from agents.validation import _merge_domain_rules, orchestrator_workflow


class ValidationWorkflowTests(unittest.TestCase):
    def test_merge_domain_rules_appends_custom_rules_for_list_payloads(self):
        merged = _merge_domain_rules(
            [
                "Emails must be lowercase.",
                {"description": "Orders with status=shipped must have shipped_at."},
            ]
        )

        self.assertEqual(merged["max_null_pct"], 0.05)
        self.assertEqual(merged["naming_convention"], "snake_case")
        self.assertEqual(
            merged["custom_rules"],
            [
                "Emails must be lowercase.",
                "Orders with status=shipped must have shipped_at.",
            ],
        )

    def test_report_to_markdown_lists_all_findings(self):
        findings = []
        for idx in range(25):
            findings.append(
                {
                    "table": f"users_{idx}",
                    "confidence": 0.81,
                    "issues": [
                        {
                            "type": "quality_issue",
                            "severity": "high",
                            "description": f"issue_{idx}",
                        }
                    ],
                }
            )

        report = {
            "summary": {
                "total_items_analyzed": 25,
                "items_passed": 0,
                "items_flagged": 25,
                "critical_issues": 0,
                "high_issues": 25,
                "medium_issues": 0,
                "low_issues": 0,
                "overall_quality_score": 45.0,
                "overall_confidence": 0.81,
                "flagged_tables": [f"users_{idx}" for idx in range(25)],
            },
            "metrics": {
                "total_columns_analyzed": 25,
            },
            "findings": findings,
        }

        markdown = report_to_markdown(report, "Column Validator Report")
        self.assertIn("### Findings (25)", markdown)
        self.assertIn("#### 25. users_24 [HIGH]", markdown)
        self.assertNotIn("... and", markdown)

    @patch("agents.validation.save_final_pipeline_report")
    @patch("agents.validation.build_validation_workflow")
    def test_orchestrator_workflow_preserves_legacy_bundle_shape(
        self,
        mock_build_workflow,
        mock_save_report,
    ):
        captured: dict[str, object] = {}

        schema_finding = Finding(
            validator="schema",
            entity="users",
            rule="missing_pk",
            severity="high",
            confidence=0.88,
            evidence={"table": "users"},
            suggested_action="Add a primary key on users.",
        )
        column_finding = Finding(
            validator="column",
            entity="users.email",
            rule="invalid_domain",
            severity="medium",
            confidence=0.82,
            evidence={"invalid_ratio": 0.2},
            suggested_action="Normalize invalid email domains.",
        )
        row_finding = Finding(
            validator="row",
            entity="users.email",
            rule="duplicate_email_rows",
            severity="low",
            confidence=0.71,
            evidence={"duplicate_rows": 3},
            suggested_action="Review duplicate rows before deduplication.",
        )
        crossrow_finding = Finding(
            validator="crossrow",
            entity="orders.user_id",
            rule="orphaned_fk",
            severity="critical",
            confidence=0.93,
            evidence={"orphaned_rows": 8},
            suggested_action="Repair or remove orphaned order rows.",
        )

        step_results = [
            SimpleNamespace(
                step_name="schema",
                content=ValidationReport(validator="schema", findings=[schema_finding]),
                steps=[],
            ),
            SimpleNamespace(
                step_name="column",
                content=ValidationReport(validator="column", findings=[column_finding]),
                steps=[],
            ),
            SimpleNamespace(
                step_name="row",
                content=ValidationReport(validator="row", findings=[row_finding]),
                steps=[],
            ),
            SimpleNamespace(
                step_name="crossrow",
                content=ValidationReport(validator="crossrow", findings=[crossrow_finding]),
                steps=[],
            ),
            SimpleNamespace(
                step_name="reduce",
                content=ReducedFindings(findings=[crossrow_finding, column_finding]),
                steps=[],
            ),
            SimpleNamespace(
                step_name="fix_plan",
                content=FixPlan(
                    cards=[
                        FixCard(
                            title="Normalize email casing",
                            target="users.email",
                            rationale="Lowercasing improves match consistency.",
                            sql="UPDATE users SET email = lower(email) WHERE email IS NOT NULL;",
                            verification_queries=[
                                "SELECT COUNT(*) FROM users WHERE email <> lower(email);"
                            ],
                            rollback_queries=[
                                "RESTORE users FROM approved backup;"
                            ],
                        )
                    ]
                ),
                steps=[],
            ),
        ]

        class _FakeWorkflow:
            def run(self, **kwargs):
                captured["run_kwargs"] = kwargs
                return SimpleNamespace(step_results=step_results)

        def _build_workflow(*, resolved_model_id, session_state, dependencies):
            captured["resolved_model_id"] = resolved_model_id
            captured["session_state"] = session_state
            captured["dependencies"] = dependencies
            return _FakeWorkflow()

        mock_build_workflow.side_effect = _build_workflow
        mock_save_report.return_value = {
            "report_file_path": "/tmp/report_localhost_5432_qualitylab.json",
            "report_file_name": "report_localhost_5432_qualitylab.json",
            "report_timestamp": "2026-04-26T12:00:00Z",
            "report_database_id": "localhost_5432_qualitylab",
        }

        result = orchestrator_workflow(
            connection_string="postgresql://user:pass@localhost:5432/qualitylab",
            domain_rules=[
                "Emails must be lowercase.",
                {"description": "Orders with status=shipped must have shipped_at."},
            ],
            llm_model="gemini-2.0-flash",
            historical_signals="Previous run saw the same orphan spike.",
        )

        self.assertEqual(captured["resolved_model_id"], "gemini-2.0-flash")
        self.assertEqual(
            captured["session_state"]["connection_string"],
            "postgresql://user:pass@localhost:5432/qualitylab",
        )
        self.assertEqual(
            captured["dependencies"]["domain_rules"]["custom_rules"],
            [
                "Emails must be lowercase.",
                "Orders with status=shipped must have shipped_at.",
            ],
        )
        self.assertIn("Historical signals", captured["run_kwargs"]["input"])

        self.assertEqual(result["llm_model"], "gemini-2.0-flash")
        self.assertEqual(result["report_file_name"], "report_localhost_5432_qualitylab.json")
        self.assertIn("schema_analysis", result)
        self.assertIn("column_validator_analysis", result)
        self.assertIn("row_level_analysis", result)
        self.assertIn("cross_row_analysis", result)
        self.assertIn("fixes", result)
        self.assertIn("agent_reports", result)
        self.assertIn("download_report_json", result)

        report_keys = set(result["download_report_json"]["reports"].keys())
        self.assertEqual(
            report_keys,
            {
                "schema_integrity_validator",
                "column_validator",
                "row_level_validator",
                "cross_row_validator",
                "data_fix_planner",
            },
        )

        fixes = result["fixes"]["fixes"]
        self.assertEqual(len(fixes), 1)
        self.assertEqual(fixes[0]["table"], "users")
        self.assertEqual(fixes[0]["target_table"], "users")
        self.assertEqual(
            fixes[0]["queries"],
            ["UPDATE users SET email = lower(email) WHERE email IS NOT NULL;"],
        )
        self.assertEqual(result["final_report"]["metrics"]["fixes_generated"], 1)
        self.assertEqual(result["download_report_json"]["summary"]["final_fix_count"], 1)


if __name__ == "__main__":
    unittest.main()
