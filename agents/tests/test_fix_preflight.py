"""
test_fix_preflight.py — Pre-flight data-safety guard for fix cards.

Mocks the database query so tests don't need a live Postgres. Each test asserts
one of the four guards: PK on duplicate column → blocked, UNIQUE on duplicate →
blocked, FK placeholder fill with missing target → blocked, pattern_not_unique
advisory → blocked.
"""
from __future__ import annotations

import unittest
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

from agents.hooks import (
    _block_card,
    _is_pattern_not_unique_card,
    _prepend_set_not_null,
    validate_fix_preflight,
)
from agents.schemas import FixCard, FixPlan


def _run_output(plan: FixPlan) -> SimpleNamespace:
    return SimpleNamespace(content=plan)


def _ctx(connection_string: str = "postgres://test", schema: dict | None = None) -> SimpleNamespace:
    return SimpleNamespace(
        session_state={
            "connection_string": connection_string,
            "schema": schema or {},
        }
    )


class PreflightPKGuardTests(unittest.TestCase):
    def test_pk_with_duplicates_is_blocked(self):
        plan = FixPlan(cards=[FixCard(
            title="Add PK to junction",
            target="campagne_contacts",
            rationale="missing pk",
            sql="ALTER TABLE campagne_contacts ADD PRIMARY KEY (campagne_id);",
        )])
        # First call → null check (returns 0 nulls). Second call → duplicate check (returns >0).
        with patch("agents.hooks._safe_query", side_effect=[0, 5]):
            validate_fix_preflight(_run_output(plan), _ctx())
        self.assertIn("BLOCKED BY PREFLIGHT", plan.cards[0].sql)
        self.assertIn("duplicate", plan.cards[0].sql.lower() + plan.cards[0].rationale.lower())

    def test_pk_with_nulls_only_gets_set_not_null_prepended(self):
        plan = FixPlan(cards=[FixCard(
            title="Add PK to campagne_contacts",
            target="campagne_contacts",
            rationale="missing pk",
            sql="ALTER TABLE campagne_contacts ADD PRIMARY KEY (campagne_id, contact_id);",
        )])
        # campagne_id: 0 nulls, contact_id: 3 nulls, dup_count: 0
        with patch("agents.hooks._safe_query", side_effect=[0, 3, 0]):
            validate_fix_preflight(_run_output(plan), _ctx())
        self.assertNotIn("BLOCKED", plan.cards[0].sql)
        self.assertIn("SET NOT NULL", plan.cards[0].sql)
        self.assertIn("contact_id", plan.cards[0].sql)
        # Should keep the original PK statement
        self.assertIn("ADD PRIMARY KEY", plan.cards[0].sql)

    def test_pk_clean_passes_unchanged(self):
        original_sql = "ALTER TABLE expediteur_groupe ADD PRIMARY KEY (groupe_id, expediteur_id);"
        plan = FixPlan(cards=[FixCard(
            title="Add PK",
            target="expediteur_groupe",
            rationale="missing pk",
            sql=original_sql,
        )])
        # No nulls, no duplicates — card unchanged.
        with patch("agents.hooks._safe_query", side_effect=[0, 0, 0]):
            validate_fix_preflight(_run_output(plan), _ctx())
        self.assertEqual(plan.cards[0].sql, original_sql)


class PreflightUniqueGuardTests(unittest.TestCase):
    def test_unique_with_duplicates_is_blocked(self):
        plan = FixPlan(cards=[FixCard(
            title="Add UNIQUE on country_code",
            target="contacts.country_code",
            rationale="advisory",
            sql="ALTER TABLE contacts ADD CONSTRAINT uk_contacts_country_code UNIQUE (country_code);",
        )])
        # 9997 duplicate non-null country_code values (FR repeats etc).
        with patch("agents.hooks._safe_query", return_value=9997):
            validate_fix_preflight(_run_output(plan), _ctx())
        self.assertIn("BLOCKED BY PREFLIGHT", plan.cards[0].sql)
        self.assertIn("UNIQUE", plan.cards[0].rationale)

    def test_unique_no_dups_passes(self):
        original_sql = "ALTER TABLE prm_organization ADD CONSTRAINT uk UNIQUE (code);"
        plan = FixPlan(cards=[FixCard(
            title="Add UNIQUE",
            target="prm_organization.code",
            rationale="real uniqueness",
            sql=original_sql,
        )])
        with patch("agents.hooks._safe_query", return_value=0):
            validate_fix_preflight(_run_output(plan), _ctx())
        self.assertEqual(plan.cards[0].sql, original_sql)


class PreflightFKPlaceholderTests(unittest.TestCase):
    def test_fk_fill_with_missing_target_is_blocked(self):
        plan = FixPlan(cards=[FixCard(
            title="Fill admin_id",
            target="groupe.admin_id",
            rationale="high null",
            sql="UPDATE groupe SET admin_id = 0 WHERE admin_id IS NULL;",
        )])
        schema = {
            "groupe": {
                "foreign_keys": [
                    {"column": "admin_id", "foreign_table": "users", "foreign_column": "id"},
                ],
            },
        }
        # EXISTS query returns False — id 0 not in users.
        with patch("agents.hooks._safe_query", return_value=False):
            validate_fix_preflight(_run_output(plan), _ctx(schema=schema))
        self.assertIn("BLOCKED BY PREFLIGHT", plan.cards[0].sql)
        self.assertIn("orphan", plan.cards[0].rationale.lower())

    def test_fk_fill_with_existing_target_passes(self):
        original_sql = "UPDATE groupe SET admin_id = 1 WHERE admin_id IS NULL;"
        plan = FixPlan(cards=[FixCard(
            title="Fill admin_id",
            target="groupe.admin_id",
            rationale="high null",
            sql=original_sql,
        )])
        schema = {
            "groupe": {
                "foreign_keys": [
                    {"column": "admin_id", "foreign_table": "users", "foreign_column": "id"},
                ],
            },
        }
        with patch("agents.hooks._safe_query", return_value=True):
            validate_fix_preflight(_run_output(plan), _ctx(schema=schema))
        self.assertEqual(plan.cards[0].sql, original_sql)


class PreflightPatternNotUniqueTests(unittest.TestCase):
    def test_pattern_not_unique_card_blocked_without_db_query(self):
        plan = FixPlan(cards=[FixCard(
            title="Add Unique Constraint to contacts.country_code",
            target="contacts.country_code",
            rationale="The column 'contacts.country_code' matches a unique pattern (`*_code`) but lacks a unique constraint.",
            sql="ALTER TABLE contacts ADD CONSTRAINT uk UNIQUE (country_code);",
        )])
        # _safe_query should NOT be called — this is a pattern-based block.
        with patch("agents.hooks._safe_query", side_effect=AssertionError("DB query should not run")):
            validate_fix_preflight(_run_output(plan), _ctx())
        self.assertIn("BLOCKED BY PREFLIGHT", plan.cards[0].sql)
        self.assertIn("pattern_not_unique", plan.cards[0].rationale.lower())


class PreflightFailsafeTests(unittest.TestCase):
    def test_no_connection_string_skips_all_checks(self):
        original_sql = "ALTER TABLE contacts ADD CONSTRAINT uk UNIQUE (country_code);"
        plan = FixPlan(cards=[FixCard(
            title="Add UNIQUE",
            target="contacts.country_code",
            rationale="advisory finding",
            sql=original_sql,
        )])
        ctx = SimpleNamespace(session_state={})
        # Even though duplicates exist, no conn means we can't verify → don't block.
        with patch("agents.hooks._safe_query") as mock_q:
            with patch("agents.hooks.postgres_tool", create=True) as mock_tool:
                mock_tool.connection_string = None
                validate_fix_preflight(_run_output(plan), ctx)
        self.assertEqual(plan.cards[0].sql, original_sql)
        mock_q.assert_not_called()


class HelperFunctionTests(unittest.TestCase):
    def test_is_pattern_not_unique_detects_rationale(self):
        card = FixCard(
            title="x", target="t", rationale="matches a unique pattern",
            sql="ALTER TABLE t ADD CONSTRAINT uk UNIQUE (c);",
        )
        self.assertTrue(_is_pattern_not_unique_card(card))

    def test_is_pattern_not_unique_false_for_real_unique(self):
        card = FixCard(
            title="x", target="t", rationale="duplicate values found",
            sql="ALTER TABLE t ADD CONSTRAINT uk UNIQUE (c);",
        )
        self.assertFalse(_is_pattern_not_unique_card(card))


if __name__ == "__main__":
    unittest.main()
