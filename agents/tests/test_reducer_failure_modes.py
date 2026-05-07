"""
test_reducer_failure_modes.py — Level 2 unit tests for reducer failure scenarios.

These tests cover paths that only activate when something goes wrong upstream
(empty profiler, partial coverage, hallucinated entities). No LLM or DB needed.
"""
import unittest
from typing import Any

from agents.reducer import ReduceSignals
from agents.schemas import Finding, ProfilerReport, ValidationReport
from agents.validation import _SyntheticStepInput


def _finding(
    entity: str,
    rule: str = "null_rate_high",
    severity: str = "high",
    confidence: float = 0.85,
    validator: str = "statistical",
    evidence: dict | None = None,
) -> Finding:
    return Finding(
        validator=validator,
        entity=entity,
        rule=rule,
        severity=severity,
        confidence=confidence,
        evidence=evidence or {"null_rate": 0.45, "row_count": 500},
        suggested_action=f"Fix {rule} on {entity}.",
    )


def _make_step_input(
    reports: dict[str, Any],
    session_state: dict[str, Any] | None = None,
) -> _SyntheticStepInput:
    return _SyntheticStepInput(reports, session_state or {})


class ReducerStatisticalCoverageGuardTests(unittest.TestCase):
    """
    Bug: statistical validator hallucinated findings for tables outside profiler coverage.
    Fix: reducer drops statistical findings when profiler_tables is non-empty but
         the finding's table is not in it.
    """

    def test_statistical_findings_for_uncovered_tables_are_dropped(self):
        # profiler only covered 2 tables; statistical claims findings on a 3rd
        session_state = {
            "profiler_report": {
                "tables": [
                    {"table_name": "users"},
                    {"table_name": "campagne"},
                ]
            },
            "schema": {
                "users":    {"columns": ["id", "email"], "column_details": []},
                "campagne": {"columns": ["id", "name"],  "column_details": []},
                "groupe":   {"columns": ["id", "label"], "column_details": []},
            },
        }
        reports = {
            "statistical": ValidationReport(
                validator="statistical",
                findings=[
                    _finding("users",    validator="statistical"),   # covered — keep
                    _finding("campagne", validator="statistical"),   # covered — keep
                    _finding("groupe",   validator="statistical"),   # NOT covered — drop
                ],
            ),
        }
        reducer = ReduceSignals(min_confidence=0.75, top_n=50)
        result = reducer(_make_step_input(reports, session_state))
        surviving = {f.entity for f in result.content.findings}

        self.assertIn("users", surviving)
        self.assertIn("campagne", surviving)
        self.assertNotIn("groupe", surviving)

    def test_statistical_findings_kept_when_profiler_fully_covers_schema(self):
        session_state = {
            "profiler_report": {
                "tables": [
                    {"table_name": "users"},
                    {"table_name": "campagne"},
                    {"table_name": "groupe"},
                ]
            },
            "schema": {
                "users":    {"columns": ["id"], "column_details": []},
                "campagne": {"columns": ["id"], "column_details": []},
                "groupe":   {"columns": ["id"], "column_details": []},
            },
        }
        reports = {
            "statistical": ValidationReport(
                validator="statistical",
                findings=[
                    _finding("users",    validator="statistical"),
                    _finding("campagne", validator="statistical"),
                    _finding("groupe",   validator="statistical"),
                ],
            ),
        }
        reducer = ReduceSignals(min_confidence=0.75, top_n=50)
        result = reducer(_make_step_input(reports, session_state))
        surviving = {f.entity for f in result.content.findings}

        self.assertIn("users", surviving)
        self.assertIn("campagne", surviving)
        self.assertIn("groupe", surviving)

    def test_statistical_findings_not_filtered_when_profiler_entirely_failed(self):
        # profiler_report absent (profiler failed completely) — guard must NOT activate
        # so that standalone statistical findings via live queries still pass through.
        session_state = {
            "schema": {
                "users": {"columns": ["id", "email"], "column_details": []},
            },
        }
        reports = {
            "statistical": ValidationReport(
                validator="statistical",
                findings=[_finding("users", validator="statistical")],
            ),
        }
        reducer = ReduceSignals(min_confidence=0.75, top_n=50)
        result = reducer(_make_step_input(reports, session_state))
        surviving = {f.entity for f in result.content.findings}

        self.assertIn("users", surviving)

    def test_non_statistical_findings_never_filtered_by_profiler_guard(self):
        # schema / structural / relational findings for uncovered tables must pass
        session_state = {
            "profiler_report": {
                "tables": [{"table_name": "users"}]
            },
            "schema": {
                "users":  {"columns": ["id"], "column_details": []},
                "groupe": {"columns": ["id"], "column_details": []},
            },
        }
        reports = {
            "schema": ValidationReport(
                validator="schema",
                findings=[_finding("groupe", validator="schema", rule="missing_pk")],
            ),
            "relational": ValidationReport(
                validator="relational",
                findings=[_finding("groupe", validator="relational", rule="orphaned_fk")],
            ),
        }
        reducer = ReduceSignals(min_confidence=0.75, top_n=50)
        result = reducer(_make_step_input(reports, session_state))
        surviving = {f.entity for f in result.content.findings}
        self.assertIn("groupe", surviving)


class ReducerEntityFilterTests(unittest.TestCase):
    """Reducer drops findings referencing tables/columns absent from the schema."""

    def test_ghost_table_finding_is_dropped(self):
        session_state = {
            "schema": {"users": {"columns": ["id"], "column_details": []}},
        }
        reports = {
            "schema": ValidationReport(
                validator="schema",
                findings=[
                    _finding("users",        validator="schema", rule="missing_pk"),
                    _finding("ghost_table",  validator="schema", rule="missing_pk"),
                ],
            ),
        }
        reducer = ReduceSignals(min_confidence=0.75, top_n=50)
        result = reducer(_make_step_input(reports, session_state))
        surviving = {f.entity for f in result.content.findings}

        self.assertIn("users", surviving)
        self.assertNotIn("ghost_table", surviving)

    def test_low_confidence_finding_is_dropped(self):
        session_state = {
            "schema": {"users": {"columns": ["id"], "column_details": []}},
        }
        reports = {
            "statistical": ValidationReport(
                validator="statistical",
                findings=[
                    _finding("users", confidence=0.5, validator="statistical"),
                ],
            ),
        }
        reducer = ReduceSignals(min_confidence=0.75, top_n=50)
        result = reducer(_make_step_input(reports, session_state))
        self.assertEqual(len(result.content.findings), 0)

    def test_zero_violation_structural_finding_is_dropped(self):
        session_state = {
            "schema": {"users": {"columns": ["id", "email"], "column_details": []}},
        }
        reports = {
            "structural": ValidationReport(
                validator="structural",
                findings=[
                    _finding(
                        "users.email",
                        rule="blank_required",
                        validator="structural",
                        evidence={"violation_count": 0},
                    ),
                ],
            ),
        }
        reducer = ReduceSignals(min_confidence=0.75, top_n=50)
        result = reducer(_make_step_input(reports, session_state))
        self.assertEqual(len(result.content.findings), 0)

    def test_empty_all_reports_returns_empty_graph(self):
        reducer = ReduceSignals(min_confidence=0.75, top_n=50)
        result = reducer(_make_step_input({}, {}))
        self.assertEqual(result.content.findings, [])
        self.assertEqual(result.content.dependency_edges, [])


class ReducerMergeAndDedupeTests(unittest.TestCase):
    """Same rule + same table findings are merged into one."""

    def test_same_rule_same_table_merged(self):
        session_state = {
            "schema": {
                "orders": {"columns": ["id", "user_id", "product_id"], "column_details": []},
            },
        }
        reports = {
            "statistical": ValidationReport(
                validator="statistical",
                findings=[
                    _finding("orders.user_id",    rule="null_rate_high", confidence=0.82),
                    _finding("orders.product_id", rule="null_rate_high", confidence=0.88),
                ],
            ),
        }
        reducer = ReduceSignals(min_confidence=0.75, top_n=50)
        result = reducer(_make_step_input(reports, session_state))
        self.assertEqual(len(result.content.findings), 1)
        surviving = result.content.findings[0]
        # Representative should be the higher-confidence one
        self.assertEqual(surviving.entity, "orders")

    def test_cross_validator_dedup_same_entity_rule_family(self):
        session_state = {
            "schema": {"users": {"columns": ["id", "email"], "column_details": []}},
        }
        reports = {
            "schema": ValidationReport(
                validator="schema",
                findings=[_finding("users.email", rule="duplicate_values",    confidence=0.90, validator="schema")],
            ),
            "statistical": ValidationReport(
                validator="statistical",
                findings=[_finding("users.email", rule="duplicate_detected",  confidence=0.85, validator="statistical")],
            ),
        }
        reducer = ReduceSignals(min_confidence=0.75, top_n=50)
        result = reducer(_make_step_input(reports, session_state))
        # Both have rule family "duplicate" on same entity → deduped to one
        entities = [f.entity for f in result.content.findings]
        self.assertEqual(entities.count("users.email"), 1)


if __name__ == "__main__":
    unittest.main()
