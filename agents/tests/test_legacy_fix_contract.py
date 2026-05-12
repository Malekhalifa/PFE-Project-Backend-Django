from __future__ import annotations

import logging
import unittest

from agents.schemas import Finding, FixCard, ReducedFindings
from agents.validation import _legacy_fix_entry


class LegacyFixContractTests(unittest.TestCase):
    def test_legacy_entry_preserves_semantic_metadata(self):
        finding = Finding(
            validator="profiler",
            entity="users.email",
            rule="null_ratio_high",
            severity="high",
            confidence=0.95,
            evidence={"null_ratio": 0.82, "total_rows": 50},
        )
        card = FixCard(
            title="Review email nulls",
            target="users.email",
            rationale="Large null rate.",
            sql="",
        )

        entry = _legacy_fix_entry(card, 1, ReducedFindings(findings=[finding]))

        self.assertEqual(entry["card_family"], "nullability")
        self.assertEqual(entry["interaction_model"], "advisory")
        self.assertEqual(entry["source_rule"], "null_ratio_high")
        self.assertEqual(entry["source_type"], "deterministic")
        self.assertEqual(entry["severity"], "high")
        self.assertEqual(entry["confidence"], 0.95)
        self.assertEqual(entry["evidence"]["null_ratio"], 0.82)
        self.assertEqual(entry["evidence_payload"]["_type"], "nullability")

    def test_blocked_card_has_no_executable_queries(self):
        card = FixCard(
            title="Fill admin_id",
            target="groupe.admin_id",
            rationale="Blocked fill.",
            sql="",
            interaction_model="blocked",
            card_family="nullability",
            source_rule="fill_nulls_blocked",
            evidence_payload={
                "_type": "blocked",
                "block_reason": "placeholder admin_id=0 missing",
                "original_sql": "UPDATE groupe SET admin_id = 0;",
            },
        )

        entry = _legacy_fix_entry(card, 1, ReducedFindings(findings=[]))

        self.assertEqual(entry["queries"], [])
        self.assertEqual(entry["interaction_model"], "blocked")
        self.assertEqual(entry["evidence_payload"]["_type"], "blocked")


class FixCardContractWarningTests(unittest.TestCase):
    def test_execute_ddl_without_rollback_logs_warning(self):
        with self.assertLogs("agents.schemas", level="WARNING") as cm:
            FixCard(
                title="Add PK",
                target="orders",
                rationale="Missing PK.",
                sql="ALTER TABLE orders ADD PRIMARY KEY (id);",
                interaction_model="execute_ddl",
                card_family="schema_design",
                rollback_queries=[],
            )
        self.assertTrue(any("no rollback_queries" in msg for msg in cm.output))

    def test_advisory_with_non_select_sql_cleared_and_warns(self):
        with self.assertLogs("agents.schemas", level="WARNING") as cm:
            card = FixCard(
                title="Review nulls",
                target="users.email",
                rationale="High null rate.",
                sql="UPDATE users SET email = 'n/a' WHERE email IS NULL;",
                interaction_model="advisory",
                card_family="nullability",
            )
        self.assertEqual(card.sql, "")
        self.assertTrue(any("non-SELECT sql" in msg for msg in cm.output))

    def test_human_decision_without_evidence_payload_logs_warning(self):
        with self.assertLogs("agents.schemas", level="WARNING") as cm:
            FixCard(
                title="Review orphans",
                target="orders",
                rationale="Orphaned FK rows.",
                sql="",
                interaction_model="human_decision",
                card_family="referential",
                evidence_payload=None,
            )
        self.assertTrue(any("missing evidence_payload" in msg for msg in cm.output))

    def test_empty_source_rule_logs_warning(self):
        card = FixCard(
            title="Investigate",
            target="users",
            rationale="Unknown issue.",
            sql="",
            interaction_model="investigate",
            card_family="unknown",
            source_rule="",
        )
        with self.assertLogs("agents.validation", level="WARNING") as cm:
            _legacy_fix_entry(card, 1, ReducedFindings(findings=[]))
        self.assertTrue(any("empty source_rule" in msg for msg in cm.output))


if __name__ == "__main__":
    unittest.main()
