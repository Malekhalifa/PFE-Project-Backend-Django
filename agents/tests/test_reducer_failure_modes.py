"""
test_reducer_failure_modes.py — Level 2 unit tests for reducer failure scenarios.

These tests cover paths that only activate when something goes wrong upstream
(empty profiler, partial coverage, hallucinated entities). No LLM or DB needed.
"""
import logging
import unittest
from typing import Any

from agents.reducer import ReduceSignals, _clean_llm_json, _try_recover_partial
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


class JsonCleanRecoveryTests(unittest.TestCase):
    """_clean_llm_json + _try_recover_partial repair common Gemini JSON glitches."""

    def test_clean_strips_trailing_commas_before_brace(self):
        text = '{"validator":"statistical","findings":[{"entity":"a","rule":"x",}]}'
        cleaned = _clean_llm_json(text)
        import json as _json
        parsed = _json.loads(cleaned)
        self.assertEqual(parsed["findings"][0]["entity"], "a")

    def test_clean_quotes_unquoted_property_keys(self):
        text = '{"validator":"statistical","findings":[{entity:"a","rule":"x","severity":"high","confidence":0.9,"evidence":{}}]}'
        cleaned = _clean_llm_json(text)
        self.assertIn('"entity"', cleaned)

    def test_clean_strips_code_fences(self):
        text = '```json\n{"validator":"statistical","findings":[]}\n```'
        cleaned = _clean_llm_json(text)
        self.assertFalse(cleaned.startswith("```"))
        self.assertFalse(cleaned.rstrip().endswith("```"))

    def test_recover_via_clean_strategy0(self):
        # Trailing comma — this is the class of failure that bit the prod run.
        # Build a finding with all required fields.
        bad = (
            '{"validator":"statistical","findings":['
            '{"validator":"statistical","entity":"users","rule":"null_rate_high",'
            '"severity":"high","confidence":0.9,"evidence":{"null_rate":0.5},}'
            ']}'
        )
        report = _try_recover_partial(bad, "statistical")
        self.assertIsNotNone(report)
        self.assertEqual(len(report.findings), 1)
        self.assertEqual(report.findings[0].entity, "users")


class ReducerSilentFailureAuditTests(unittest.TestCase):
    """Validator step with tool calls > 0 but unparseable output → ERROR log."""

    def test_unparseable_step_with_tool_calls_triggers_error_log(self):
        session_state = {
            "schema": {"users": {"columns": ["id"], "column_details": []}},
            # Primary source — stable counter dict written by log_run_metrics post-hook.
            "_validator_tool_calls": {"statistical-validator": 2},
        }
        # statistical step returned unparseable garbage — reducer falls back to skip.
        reports = {
            "statistical": "this is not json at all { broken",
        }
        reducer = ReduceSignals(min_confidence=0.75, top_n=50)
        with self.assertLogs("agents.reducer", level="ERROR") as cm:
            reducer(_make_step_input(reports, session_state))
        joined = "\n".join(cm.output)
        self.assertIn("SILENT FAILURE", joined)
        self.assertIn("statistical", joined)

    def test_unparseable_with_legacy_seen_dict_still_works(self):
        # Backward-compat: when only the legacy _tool_calls_seen_<aid> dict is present
        # (no _validator_tool_calls counter), audit still fires.
        session_state = {
            "schema": {"users": {"columns": ["id"], "column_details": []}},
            "_tool_calls_seen_statistical-validator": {"a": 1, "b": 1},
        }
        reports = {"statistical": "broken{ json"}
        reducer = ReduceSignals(min_confidence=0.75, top_n=50)
        with self.assertLogs("agents.reducer", level="ERROR") as cm:
            reducer(_make_step_input(reports, session_state))
        self.assertIn("SILENT FAILURE", "\n".join(cm.output))

    def test_empty_findings_with_tool_calls_triggers_warning(self):
        # Step parsed cleanly but emitted 0 findings while tools ran — suspicious but valid.
        session_state = {
            "schema": {"users": {"columns": ["id"], "column_details": []}},
            "_validator_tool_calls": {"structural-validator": 1},
        }
        reports = {
            "structural": ValidationReport(validator="structural", findings=[]),
        }
        reducer = ReduceSignals(min_confidence=0.75, top_n=50)
        with self.assertLogs("agents.reducer", level="WARNING") as cm:
            reducer(_make_step_input(reports, session_state))
        joined = "\n".join(cm.output)
        self.assertIn("0 findings", joined)
        self.assertIn("structural", joined)

    def test_empty_findings_without_tool_calls_no_warning(self):
        # Step ran 0 tool calls (e.g., short-circuited) and produced 0 findings → don't warn.
        session_state = {
            "schema": {"users": {"columns": ["id"], "column_details": []}},
        }
        reports = {
            "relational": ValidationReport(validator="relational", findings=[]),
        }
        reducer = ReduceSignals(min_confidence=0.75, top_n=50)
        # Use a logger.disabled trick: capture all logs and assert no SILENT/0 findings entries.
        logger = logging.getLogger("agents.reducer")
        with self.assertLogs(logger, level="DEBUG") as cm:
            reducer(_make_step_input(reports, session_state))
            # Force at least one log so assertLogs doesn't raise.
            logger.debug("sentinel")
        joined = "\n".join(cm.output)
        self.assertNotIn("SILENT FAILURE", joined)
        self.assertNotIn("ran 0 tool call", joined)


class MisLabeledValidatorTests(unittest.TestCase):
    """Reducer must look up reports by step_name, not by report.validator field.

    The LLM has been observed setting validator='profiler' on the statistical
    step's output. Earlier reducer code did
        next((r for r in reports if r.validator == 'statistical'), None)
    which returned None and silently skipped the auto-finding merge.
    """

    def test_mislabeled_statistical_report_still_merges_auto_findings(self):
        # Statistical step emits a report with validator='profiler' (mis-label).
        # py_profiler has 1 deterministic statistical auto-finding to merge.
        bad_stat_report = ValidationReport(
            validator="profiler",  # WRONG — should be "statistical"
            findings=[],
        )
        # _SyntheticStepInput uses dict keys as step_name lookup
        reports = {"statistical": bad_stat_report}
        session_state = {
            "schema": {"groupe": {"columns": ["id", "admin_id"], "column_details": []}},
            "profiler_auto_findings": [
                {
                    "validator": "statistical",
                    "entity": "groupe.admin_id",
                    "rule": "null_ratio_high",
                    "severity": "high",
                    "confidence": 0.95,
                    "evidence": {"null_ratio": 0.89},
                },
            ],
        }
        reducer = ReduceSignals(min_confidence=0.75, top_n=50)
        result = reducer(_make_step_input(reports, session_state))
        # The deterministic auto-finding must survive the mis-label and reach
        # the final ranked output.
        entities = {f.entity for f in result.content.findings}
        self.assertIn("groupe.admin_id", entities)

    def test_correctly_labeled_statistical_report_unchanged(self):
        good_stat_report = ValidationReport(
            validator="statistical",
            findings=[
                _finding("orders.email", validator="statistical"),
            ],
        )
        reports = {"statistical": good_stat_report}
        session_state = {
            "schema": {"orders": {"columns": ["id", "email"], "column_details": []}},
        }
        reducer = ReduceSignals(min_confidence=0.75, top_n=50)
        result = reducer(_make_step_input(reports, session_state))
        entities = {f.entity for f in result.content.findings}
        self.assertIn("orders.email", entities)


class TableLevelMergeEvidenceTests(unittest.TestCase):
    """When same-rule findings on the same table are merged, the representative's
    evidence['columns_with_high_nulls'] must NOT contain empty strings.

    Bug seen in prod: missing_pk findings have entity='campagne_contacts' (table
    only, no '.column' part). Falling back to lead_col='' put an empty string
    first in columns_with_high_nulls, and the LLM fix planner read that as
    ambiguous evidence and dropped the entire critical PK fix card."""

    def test_table_level_merge_no_empty_string_in_columns(self):
        # Two missing_pk findings for the same table at table level (entity has no dot).
        f1 = _finding("campagne_contacts", rule="missing_pk", severity="critical",
                      confidence=1.0, validator="schema",
                      evidence={"primary_key_required": True})
        f2 = _finding("campagne_contacts", rule="missing_pk", severity="critical",
                      confidence=1.0, validator="schema",
                      evidence={"primary_key_required": True})
        # Distinct keys via different evidence so reducer keeps both.
        f2.evidence = {"primary_key_required": True, "extra": "x"}
        session_state = {
            "schema": {"campagne_contacts": {"columns": ["id", "campagne_id"], "column_details": []}},
        }
        reports = {"schema": ValidationReport(validator="schema", findings=[f1, f2])}
        reducer = ReduceSignals(min_confidence=0.75, top_n=50)
        result = reducer(_make_step_input(reports, session_state))

        for f in result.content.findings:
            cols = (f.evidence or {}).get("columns_with_high_nulls") or []
            for c in cols:
                self.assertNotEqual(c, "", f"empty-string column in evidence for {f.entity}")
                self.assertIsNotNone(c, f"None column in evidence for {f.entity}")

    def test_column_level_merge_keeps_lead_col(self):
        f1 = _finding("smsc_routing_rule.expediteur_id", rule="null_rate_high",
                      severity="high", validator="statistical")
        f2 = _finding("smsc_routing_rule.groupe_id", rule="null_rate_high",
                      severity="high", validator="statistical")
        session_state = {
            "schema": {"smsc_routing_rule": {"columns": ["id", "expediteur_id", "groupe_id"],
                                              "column_details": []}},
        }
        reports = {"statistical": ValidationReport(validator="statistical", findings=[f1, f2])}
        reducer = ReduceSignals(min_confidence=0.75, top_n=50)
        result = reducer(_make_step_input(reports, session_state))
        merged = next(f for f in result.content.findings if f.entity == "smsc_routing_rule")
        cols = (merged.evidence or {}).get("columns_with_high_nulls") or []
        # Both columns present, neither empty.
        self.assertIn("expediteur_id", cols)
        self.assertIn("groupe_id", cols)
        self.assertNotIn("", cols)


if __name__ == "__main__":
    unittest.main()
