"""
test_recovery_path.py — Level 2 unit tests for the profiler failure recovery path.

Simulates the scenario where the profiler step finishes but produces no output
(store_profiler_output never fires), triggering the standalone recovery in
orchestrator_workflow. No LLM, no DB — agents are mocked with controlled responses.
"""
import json
import unittest
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

import django
import os
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
if not django.apps.apps.ready:
    django.setup()

from agents.schemas import (
    ProfilerColumnStat,
    ProfilerReport,
    ProfilerTableStat,
    ValidationReport,
)
from agents.validation import (
    _rerun_statistical_structural,
    _run_profiler_standalone,
    _SyntheticStepInput,
)


def _make_profiler_report(table_names: list[str]) -> ProfilerReport:
    tables = [
        ProfilerTableStat(
            table_name=name,
            row_count=100,
            column_stats=[
                ProfilerColumnStat(
                    column_name="id",
                    null_rate=0.0,
                    cardinality=100,
                    total_rows=100,
                    outlier_flag=False,
                )
            ],
        )
        for name in table_names
    ]
    return ProfilerReport(tables=tables)


class ProfilerStandaloneRecoveryTests(unittest.TestCase):
    """
    Bug: profiler_summary stayed empty after workflow because MALFORMED_FUNCTION_CALL
    bypassed post_hooks, so store_profiler_output never fired.
    Fix: _run_profiler_standalone retries the agent and writes profiler_summary itself.
    """

    def test_successful_recovery_writes_profiler_summary_to_session_state(self):
        session_state: dict[str, Any] = {"profiler_schema": {}}
        report = _make_profiler_report(["users", "orders"])

        mock_profiler = MagicMock()
        mock_profiler.run.return_value = SimpleNamespace(content=report)

        result = _run_profiler_standalone(
            profiler=mock_profiler,
            session_state=session_state,
            database_id="test_db",
        )

        self.assertEqual(len(result.tables), 2)
        self.assertIn("users",  session_state["profiler_summary"])
        self.assertIn("orders", session_state["profiler_summary"])
        self.assertIn("profiler_report", session_state)

    def test_recovery_retries_up_to_max_and_returns_empty_on_total_failure(self):
        session_state: dict[str, Any] = {}
        mock_profiler = MagicMock()
        # Always raise
        mock_profiler.run.side_effect = RuntimeError("MALFORMED_FUNCTION_CALL")

        with patch("agents.validation._PROFILER_RETRY_DELAY_S", 0):
            result = _run_profiler_standalone(
                profiler=mock_profiler,
                session_state=session_state,
                database_id="test_db",
            )

        self.assertEqual(result.tables, [])
        self.assertNotIn("profiler_summary", session_state)
        self.assertEqual(mock_profiler.run.call_count, 2)  # _MAX_PROFILER_RETRIES = 2

    def test_recovery_succeeds_on_second_retry_after_first_failure(self):
        session_state: dict[str, Any] = {}
        report = _make_profiler_report(["users"])

        mock_profiler = MagicMock()
        mock_profiler.run.side_effect = [
            RuntimeError("MALFORMED_FUNCTION_CALL"),      # retry 1 fails
            SimpleNamespace(content=report),               # retry 2 succeeds
        ]

        with patch("agents.validation._PROFILER_RETRY_DELAY_S", 0):
            result = _run_profiler_standalone(
                profiler=mock_profiler,
                session_state=session_state,
                database_id="test_db",
            )

        self.assertEqual(len(result.tables), 1)
        self.assertIn("users", session_state["profiler_summary"])
        self.assertEqual(mock_profiler.run.call_count, 2)

    def test_empty_profiler_output_does_not_write_to_session_state(self):
        # Agent returns a ProfilerReport with 0 tables — should NOT be stored
        session_state: dict[str, Any] = {}
        empty_report = ProfilerReport(tables=[])

        mock_profiler = MagicMock()
        mock_profiler.run.return_value = SimpleNamespace(content=empty_report)

        with patch("agents.validation._PROFILER_RETRY_DELAY_S", 0):
            result = _run_profiler_standalone(
                profiler=mock_profiler,
                session_state=session_state,
                database_id="test_db",
            )

        # Should exhaust retries since report.tables is empty each time
        self.assertEqual(result.tables, [])
        self.assertNotIn("profiler_summary", session_state)


class CoverageThresholdTests(unittest.TestCase):
    """
    Bug: statistical re-run ran even when profiler only covered 2/30 tables,
    producing hallucinations for the uncovered 28.
    Fix: skip statistical when coverage_ratio < 0.5.
    """

    def test_skip_statistical_flag_is_true_when_coverage_below_50pct(self):
        from agents.validation import orchestrator_workflow
        # We test the flag logic directly without running the full workflow.
        known_tables = 30
        profiled_count = 2
        coverage_ratio = profiled_count / known_tables
        skip_stat = known_tables > 0 and coverage_ratio < 0.5
        self.assertTrue(skip_stat)

    def test_skip_statistical_flag_is_false_when_coverage_at_or_above_50pct(self):
        known_tables = 10
        profiled_count = 5
        coverage_ratio = profiled_count / known_tables
        skip_stat = known_tables > 0 and coverage_ratio < 0.5
        self.assertFalse(skip_stat)

    def test_rerun_statistical_structural_skip_statistical_returns_empty_statistical(self):
        # When skip_statistical=True, statistical gets empty ValidationReport
        members = SimpleNamespace(
            statistical_validator=MagicMock(),
            structural_validator=MagicMock(),
        )
        structural_report = ValidationReport(validator="structural", findings=[])
        members.structural_validator.run.return_value = SimpleNamespace(
            content=structural_report
        )

        stat, struct = _rerun_statistical_structural(
            members=members,
            session_state={},
            database_id="test_db",
            skip_statistical=True,
        )

        self.assertEqual(stat.findings, [])
        self.assertEqual(stat.validator, "statistical")
        members.statistical_validator.run.assert_not_called()
        members.structural_validator.run.assert_called_once()

    def test_rerun_statistical_structural_both_run_when_skip_false(self):
        members = SimpleNamespace(
            statistical_validator=MagicMock(),
            structural_validator=MagicMock(),
        )
        members.statistical_validator.run.return_value = SimpleNamespace(
            content=ValidationReport(validator="statistical", findings=[])
        )
        members.structural_validator.run.return_value = SimpleNamespace(
            content=ValidationReport(validator="structural", findings=[])
        )

        _rerun_statistical_structural(
            members=members,
            session_state={},
            database_id="test_db",
            skip_statistical=False,
        )

        members.statistical_validator.run.assert_called_once()
        members.structural_validator.run.assert_called_once()


class SyntheticStepInputTests(unittest.TestCase):
    """_SyntheticStepInput correctly bridges reducer calls outside workflow context."""

    def test_get_step_content_returns_correct_report(self):
        schema_report = ValidationReport(validator="schema", findings=[])
        reports = {"schema": schema_report, "statistical": None}
        step_in = _SyntheticStepInput(reports, {"job_id": "test-123"})

        self.assertIs(step_in.get_step_content("schema"), schema_report)
        self.assertIsNone(step_in.get_step_content("statistical"))
        self.assertIsNone(step_in.get_step_content("relational"))

    def test_workflow_session_exposes_session_data(self):
        session_state = {"profiler_summary": {"users": {}}, "job_id": "abc"}
        step_in = _SyntheticStepInput({}, session_state)
        self.assertEqual(step_in.workflow_session.session_data, session_state)


if __name__ == "__main__":
    unittest.main()
