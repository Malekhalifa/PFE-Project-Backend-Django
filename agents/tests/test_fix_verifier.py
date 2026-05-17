"""
test_fix_verifier.py — unit tests for the Fix Planner post-processor.

Tests are DB-free: _run_select is monkey-patched to return controlled results.
Covers all five passes: verification execution, contradiction detection,
DELETE overlap detection, source_rule backfill, and evidence_payload column fix.
"""
import unittest
from unittest.mock import patch

from agents.schemas import FixCard, FixPlan, Finding
from agents.fix_verifier import (
    _backfill_source_rules,
    _contradiction_check,
    _extract_count,
    _extract_delete_table,
    _extract_where_predicate,
    _fix_evidence_payload_columns,
    _is_dismissing,
    _overlap_detection,
    _run_verification_queries,
    _sync_severity_from_findings,
    enrich_fix_plan,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _card(fix_id, target, sql, source_rule="", rationale="Fix needed.",
          interaction_model="human_decision", evidence_payload=None,
          depends_on=None):
    return FixCard(
        fix_id=fix_id,
        title=fix_id,
        target=target,
        rationale=rationale,
        sql=sql,
        source_rule=source_rule,
        interaction_model=interaction_model,
        evidence_payload=evidence_payload,
        depends_on=depends_on or [],
    )


def _finding(entity, rule, null_ratio=None, validator="schema"):
    ev = {"null_ratio": null_ratio} if null_ratio is not None else {}
    table = entity.split(".")[0]
    return Finding(
        table=table,
        entity=entity,
        rule=rule,
        severity="high",
        confidence=0.9,
        evidence=ev,
        validator=validator,
    )


# ---------------------------------------------------------------------------
# Pass 1 — verification execution
# ---------------------------------------------------------------------------

class TestRunVerificationQueries(unittest.TestCase):

    def test_results_injected_into_card(self):
        card = _card("c1", "users", "SELECT 1",
                     rationale="r", interaction_model="investigate")
        card.verification_queries = ["SELECT COUNT(*) FROM users WHERE email IS NULL;"]

        fake_rows = [{"count": 1500}]
        with patch("agents.fix_verifier._run_select", return_value=fake_rows):
            _run_verification_queries([card], "fake://conn")

        self.assertIsNotNone(card.verification_results)
        results = card.verification_results
        self.assertEqual(len(results), 1)
        stored = list(results.values())[0]
        self.assertEqual(stored, fake_rows)

    def test_no_verification_queries_skipped(self):
        card = _card("c2", "users", "SELECT 1")
        card.verification_queries = []
        with patch("agents.fix_verifier._run_select") as mock_run:
            _run_verification_queries([card], "fake://conn")
        mock_run.assert_not_called()
        self.assertIsNone(card.verification_results)

    def test_db_error_stored_as_error_dict(self):
        card = _card("c3", "users", "SELECT 1")
        card.verification_queries = ["SELECT COUNT(*) FROM users WHERE email IS NULL;"]
        with patch("agents.fix_verifier._run_select", return_value=None):
            _run_verification_queries([card], "fake://conn")
        stored = list(card.verification_results.values())[0]
        self.assertIn("error", stored)


# ---------------------------------------------------------------------------
# Pass 2 — contradiction detection
# ---------------------------------------------------------------------------

class TestContradictionCheck(unittest.TestCase):

    def _make_dismiss_card(self, null_ratio=0.15):
        return _card(
            "null-ratio-users-email", "users.email",
            sql="SELECT COUNT(*) FROM users WHERE email IS NULL;",
            rationale="Re-evaluation shows no nulls. No action needed.",
            interaction_model="investigate",
            evidence_payload={"null_ratio": null_ratio, "null_count": 1500, "total_rows": 10000},
        )

    def test_dismiss_with_high_null_ratio_overridden(self):
        card = self._make_dismiss_card(null_ratio=0.15)
        _contradiction_check([card], {})
        self.assertEqual(card.interaction_model, "investigate")
        self.assertIn("SYSTEM OVERRIDE", card.rationale)
        self.assertIn("null_ratio=0.15", card.rationale)
        self.assertEqual(card.risk_level, "low")

    def test_low_null_ratio_not_overridden(self):
        card = self._make_dismiss_card(null_ratio=0.02)
        original_rationale = card.rationale
        _contradiction_check([card], {})
        self.assertEqual(card.rationale, original_rationale)

    def test_non_dismissing_rationale_not_overridden(self):
        card = _card(
            "delete-nulls", "users.email",
            sql="DELETE FROM users WHERE email IS NULL;",
            rationale="Delete rows where email is null to enforce NOT NULL constraint.",
            evidence_payload={"null_ratio": 0.15},
        )
        original_rationale = card.rationale
        _contradiction_check([card], {})
        self.assertEqual(card.rationale, original_rationale)

    def test_uses_evidence_by_entity_fallback(self):
        """Card with no evidence_payload → evidence_by_entity lookup."""
        card = _card(
            "null-check", "groupe.admin_id",
            sql="SELECT COUNT(*) FROM groupe WHERE admin_id IS NULL;",
            rationale="Re-evaluation shows no nulls. No action needed.",
            interaction_model="investigate",
        )
        evidence_by_entity = {"groupe.admin_id": {"null_ratio": 0.89}}
        _contradiction_check([card], evidence_by_entity)
        self.assertIn("SYSTEM OVERRIDE", card.rationale)

    def test_real_verification_count_included_in_override(self):
        card = self._make_dismiss_card()
        card.verification_results = {
            "SELECT COUNT(*) FROM users WHERE email IS NULL;": [{"count": 1500}]
        }
        _contradiction_check([card], {})
        self.assertIn("1500", card.rationale)


# ---------------------------------------------------------------------------
# Pass 3 — DELETE overlap detection
# ---------------------------------------------------------------------------

class TestOverlapDetection(unittest.TestCase):

    def _smsc_cards(self):
        card_a = _card(
            "delete-null-smsc-expediteur-id",
            "smsc_routing_rule.expediteur_id",
            sql="DELETE FROM smsc_routing_rule WHERE expediteur_id IS NULL;",
        )
        card_b = _card(
            "delete-null-smsc-groupe-id",
            "smsc_routing_rule.groupe_id",
            sql="DELETE FROM smsc_routing_rule WHERE groupe_id IS NULL;",
        )
        return card_a, card_b

    def test_overlap_injects_depends_on(self):
        card_a, card_b = self._smsc_cards()
        # 6 rows have both columns null → overlap exists.
        with patch("agents.fix_verifier._run_select", return_value=[{"n": 6}]):
            _overlap_detection([card_a, card_b], "fake://conn")
        self.assertIn(card_a.fix_id, card_b.depends_on)
        self.assertNotIn(card_b.fix_id, card_a.depends_on)

    def test_no_overlap_no_dependency_injected(self):
        card_a, card_b = self._smsc_cards()
        # 0 shared rows — no overlap.
        with patch("agents.fix_verifier._run_select", return_value=[{"n": 0}]):
            _overlap_detection([card_a, card_b], "fake://conn")
        self.assertEqual(card_b.depends_on, [])

    def test_existing_dependency_not_duplicated(self):
        card_a, card_b = self._smsc_cards()
        card_b.depends_on = [card_a.fix_id]  # already linked
        with patch("agents.fix_verifier._run_select") as mock_run:
            _overlap_detection([card_a, card_b], "fake://conn")
        mock_run.assert_not_called()

    def test_single_delete_card_skipped(self):
        card_a, _ = self._smsc_cards()
        with patch("agents.fix_verifier._run_select") as mock_run:
            _overlap_detection([card_a], "fake://conn")
        mock_run.assert_not_called()

    def test_different_tables_not_compared(self):
        card_a = _card("d1", "table_a.col",
                       sql="DELETE FROM table_a WHERE col IS NULL;")
        card_b = _card("d2", "table_b.col",
                       sql="DELETE FROM table_b WHERE col IS NULL;")
        with patch("agents.fix_verifier._run_select") as mock_run:
            _overlap_detection([card_a, card_b], "fake://conn")
        mock_run.assert_not_called()

    def test_db_error_skips_pair_gracefully(self):
        card_a, card_b = self._smsc_cards()
        with patch("agents.fix_verifier._run_select", return_value=None):
            _overlap_detection([card_a, card_b], "fake://conn")
        # No exception, no dependency injected.
        self.assertEqual(card_b.depends_on, [])

    def test_non_delete_cards_ignored(self):
        card_a, card_b = self._smsc_cards()
        ddl_card = _card("add-index", "smsc_routing_rule.col",
                         sql="CREATE INDEX IF NOT EXISTS idx ON smsc_routing_rule (col);")
        with patch("agents.fix_verifier._run_select", return_value=[{"n": 6}]):
            _overlap_detection([card_a, card_b, ddl_card], "fake://conn")
        # Only card_a and card_b compared; ddl_card untouched.
        self.assertNotIn(card_a.fix_id, ddl_card.depends_on)
        self.assertNotIn(card_b.fix_id, ddl_card.depends_on)


# ---------------------------------------------------------------------------
# Pass 4 — source_rule backfill
# ---------------------------------------------------------------------------

class TestBackfillSourceRules(unittest.TestCase):

    def test_exact_entity_match(self):
        card = _card("c1", "groupe.admin_id",
                     sql="DELETE FROM groupe WHERE admin_id IS NULL;")
        f = _finding("groupe.admin_id", "null_ratio_high", null_ratio=0.89)
        _backfill_source_rules([card], [f])
        self.assertEqual(card.source_rule, "null_ratio_high")

    def test_table_level_fallback(self):
        card = _card("c2", "campagne_contacts",
                     sql="ALTER TABLE campagne_contacts ADD PRIMARY KEY (campagne_id, contact_id);")
        f = _finding("campagne_contacts", "missing_pk")
        _backfill_source_rules([card], [f])
        self.assertEqual(card.source_rule, "missing_pk")

    def test_card_with_table_col_target_uses_table_fallback(self):
        card = _card("c3", "smsc_routing_rule.groupe_id",
                     sql="DELETE FROM smsc_routing_rule WHERE groupe_id IS NULL;")
        # Finding entity matches exactly.
        f = _finding("smsc_routing_rule.groupe_id", "foreign_key_validation")
        _backfill_source_rules([card], [f])
        self.assertEqual(card.source_rule, "foreign_key_validation")

    def test_existing_source_rule_not_overwritten(self):
        card = _card("c4", "users.email", sql="SELECT 1", source_rule="existing_rule")
        f = _finding("users.email", "null_ratio_high")
        _backfill_source_rules([card], [f])
        self.assertEqual(card.source_rule, "existing_rule")

    def test_no_matching_finding_leaves_empty(self):
        card = _card("c5", "unknown_table.col", sql="SELECT 1")
        f = _finding("other_table.col", "some_rule")
        _backfill_source_rules([card], [f])
        self.assertEqual(card.source_rule, "")

    def test_multiple_cards_backfilled_in_one_pass(self):
        cards = [
            _card("c6", "groupe.admin_id", sql="DELETE FROM groupe WHERE admin_id IS NULL;"),
            _card("c7", "smsc_routing_rule.expediteur_id",
                  sql="DELETE FROM smsc_routing_rule WHERE expediteur_id IS NULL;"),
        ]
        findings = [
            _finding("groupe.admin_id", "null_ratio_high"),
            _finding("smsc_routing_rule.expediteur_id", "null_rate_high"),
        ]
        _backfill_source_rules(cards, findings)
        self.assertEqual(cards[0].source_rule, "null_ratio_high")
        self.assertEqual(cards[1].source_rule, "null_rate_high")


# ---------------------------------------------------------------------------
# Helpers unit tests
# ---------------------------------------------------------------------------

class TestHelpers(unittest.TestCase):

    def test_is_dismissing(self):
        self.assertTrue(_is_dismissing("Re-evaluation shows no nulls. No action needed."))
        self.assertTrue(_is_dismissing("result is 0, no issues found"))
        self.assertTrue(_is_dismissing("shows no violations"))
        self.assertFalse(_is_dismissing("Delete rows where admin_id IS NULL to enforce NOT NULL."))

    def test_extract_delete_table(self):
        self.assertEqual(
            _extract_delete_table("DELETE FROM smsc_routing_rule WHERE expediteur_id IS NULL;"),
            "smsc_routing_rule",
        )
        self.assertEqual(
            _extract_delete_table('DELETE FROM "groupe" WHERE admin_id IS NULL;'),
            "groupe",
        )
        self.assertIsNone(_extract_delete_table("ALTER TABLE foo ADD PRIMARY KEY (id);"))

    def test_extract_where_predicate(self):
        self.assertEqual(
            _extract_where_predicate("DELETE FROM smsc_routing_rule WHERE expediteur_id IS NULL;"),
            "expediteur_id IS NULL",
        )
        self.assertEqual(
            _extract_where_predicate("DELETE FROM t WHERE col_a IS NULL AND col_b IS NULL;"),
            "col_a IS NULL AND col_b IS NULL",
        )
        self.assertIsNone(_extract_where_predicate("ALTER TABLE t ADD PRIMARY KEY (id);"))

    def test_extract_count(self):
        self.assertEqual(_extract_count([{"count": 8}]), 8)
        self.assertEqual(_extract_count([{"n": 0}]), 0)
        self.assertIsNone(_extract_count(None))
        self.assertIsNone(_extract_count([]))

    def test_extract_count_various_column_names(self):
        self.assertEqual(_extract_count([{"count": 6}]), 6)
        self.assertEqual(_extract_count([{"n": 6}]), 6)
        self.assertEqual(_extract_count([{"total": 6}]), 6)


# ---------------------------------------------------------------------------
# enrich_fix_plan — integration (mocked DB)
# ---------------------------------------------------------------------------

class TestEnrichFixPlan(unittest.TestCase):

    def test_full_pipeline_bc584c62_scenario(self):
        """Reproduce the bc584c62 failures in one shot."""
        cards = [
            # Confabulation case (users.email)
            _card(
                "null-ratio-users-email", "users.email",
                sql="SELECT COUNT(*) FROM users WHERE email IS NULL;",
                rationale="Re-evaluation shows no nulls. No action needed.",
                interaction_model="investigate",
                evidence_payload={"null_ratio": 0.15, "null_count": 1500},
            ),
            # DELETE overlap case
            _card(
                "delete-null-smsc-expediteur-id", "smsc_routing_rule.expediteur_id",
                sql="DELETE FROM smsc_routing_rule WHERE expediteur_id IS NULL;",
            ),
            _card(
                "delete-null-smsc-groupe-id", "smsc_routing_rule.groupe_id",
                sql="DELETE FROM smsc_routing_rule WHERE groupe_id IS NULL;",
            ),
            # Empty source_rule case
            _card(
                "set-not-null-campagne-schedule-id", "campagne.schedule_id",
                sql="ALTER TABLE campagne ALTER COLUMN schedule_id SET NOT NULL;",
                source_rule="",
            ),
        ]
        findings = [
            _finding("smsc_routing_rule.expediteur_id", "null_rate_high", null_ratio=0.857),
            _finding("smsc_routing_rule.groupe_id", "nullable_fk", null_ratio=0.857),
            _finding("campagne.schedule_id", "foreign_key_validation"),
        ]
        plan = FixPlan(cards=cards)

        def fake_run_select(conn_str, sql):
            if "expediteur_id IS NULL" in sql and "groupe_id IS NULL" in sql:
                return [{"n": 6}]  # overlap
            if "email IS NULL" in sql:
                return [{"count": 1500}]
            return [{"n": 0}]

        with patch("agents.fix_verifier._run_select", side_effect=fake_run_select):
            enrich_fix_plan(plan, findings=findings, connection_string="fake://conn")

        email_card = next(c for c in plan.cards if c.fix_id == "null-ratio-users-email")
        smsc_b = next(c for c in plan.cards if c.fix_id == "delete-null-smsc-groupe-id")
        campagne_card = next(c for c in plan.cards if c.fix_id == "set-not-null-campagne-schedule-id")

        # Pass 2: confabulation overridden
        self.assertIn("SYSTEM OVERRIDE", email_card.rationale)
        self.assertEqual(email_card.risk_level, "low")

        # Pass 3: overlap dependency injected
        self.assertIn("delete-null-smsc-expediteur-id", smsc_b.depends_on)

        # Pass 4: source_rule backfilled
        self.assertEqual(campagne_card.source_rule, "foreign_key_validation")

    def test_empty_plan_returns_cleanly(self):
        plan = FixPlan(cards=[])
        result = enrich_fix_plan(plan, findings=[], connection_string="fake://conn")
        self.assertIs(result, plan)


# ---------------------------------------------------------------------------
# Pass 5: evidence_payload column correction
# ---------------------------------------------------------------------------

class TestFixEvidencePayloadColumns(unittest.TestCase):

    def test_mismatched_column_corrected(self):
        """LLM copy-pasted evidence_payload from first FK card to subsequent cards."""
        card = _card(
            "advisory-campagne-expediteur-id", "campagne.expediteur_id",
            sql="SELECT * FROM campagne WHERE expediteur_id IS NULL LIMIT 10;",
            evidence_payload={"table_name": "campagne", "column_name": "user_id"},  # wrong col
        )
        _fix_evidence_payload_columns([card])
        self.assertEqual(card.evidence_payload["column_name"], "expediteur_id")

    def test_matching_column_unchanged(self):
        card = _card(
            "advisory-campagne-user-id", "campagne.user_id",
            sql="SELECT * FROM campagne WHERE user_id IS NULL LIMIT 10;",
            evidence_payload={"table_name": "campagne", "column_name": "user_id"},
        )
        _fix_evidence_payload_columns([card])
        self.assertEqual(card.evidence_payload["column_name"], "user_id")

    def test_table_level_target_skipped(self):
        """No '.' in target — no column to infer, skip."""
        card = _card(
            "add-pk-campagne-contacts", "campagne_contacts",
            sql="ALTER TABLE campagne_contacts ADD PRIMARY KEY (campagne_id, contact_id);",
            evidence_payload={"table_name": "campagne_contacts", "column_name": "campagne_id"},
        )
        original_col = card.evidence_payload["column_name"]
        _fix_evidence_payload_columns([card])
        self.assertEqual(card.evidence_payload["column_name"], original_col)

    def test_no_evidence_payload_skipped(self):
        card = _card("foo", "table.col", sql="SELECT 1;", evidence_payload=None)
        _fix_evidence_payload_columns([card])  # must not raise
        self.assertIsNone(card.evidence_payload)

    def test_batch_multiple_cards(self):
        """Simulate 5 campagne FK cards all copy-pasted from user_id."""
        cols = ["expediteur_id", "modele_id", "campaign_type_permission_id", "schedule_id"]
        cards = [
            _card(
                f"advisory-campagne-{col}", f"campagne.{col}",
                sql=f"SELECT * FROM campagne WHERE {col} IS NULL LIMIT 10;",
                evidence_payload={"table_name": "campagne", "column_name": "user_id"},
            )
            for col in cols
        ]
        _fix_evidence_payload_columns(cards)
        for card, col in zip(cards, cols):
            self.assertEqual(card.evidence_payload["column_name"], col, f"failed for {col}")


# ---------------------------------------------------------------------------
# FixCard._RULE_TO_FAMILY auto-derive (schemas.py model_validator)
# ---------------------------------------------------------------------------

class TestCardFamilyAutoDerive(unittest.TestCase):

    def _make_card(self, source_rule, card_family="unknown"):
        return FixCard(
            fix_id="test",
            title="test",
            target="some_table",
            rationale="test",
            sql="SELECT 1;",
            source_rule=source_rule,
            card_family=card_family,
        )

    def test_missing_primary_key_derives_schema_design(self):
        card = self._make_card("missing_primary_key")
        self.assertEqual(card.card_family, "schema_design")

    def test_missing_index_derives_schema_design(self):
        card = self._make_card("missing_index")
        self.assertEqual(card.card_family, "schema_design")

    def test_nullable_fk_derives_nullability(self):
        card = self._make_card("nullable_fk")
        self.assertEqual(card.card_family, "nullability")

    def test_null_rate_high_derives_nullability(self):
        card = self._make_card("null_rate_high")
        self.assertEqual(card.card_family, "nullability")

    def test_duplicate_values_derives_duplicates(self):
        card = self._make_card("duplicate_values")
        self.assertEqual(card.card_family, "duplicates")

    def test_orphaned_fk_derives_referential(self):
        card = self._make_card("orphaned_fk")
        self.assertEqual(card.card_family, "referential")

    def test_categorical_drift_derives_statistical(self):
        card = self._make_card("categorical_drift")
        self.assertEqual(card.card_family, "statistical")

    def test_unknown_rule_stays_unknown(self):
        card = self._make_card("some_unrecognized_rule")
        self.assertEqual(card.card_family, "unknown")

    def test_explicit_family_not_overridden(self):
        """If LLM correctly set card_family, auto-derive must not overwrite it."""
        card = self._make_card("missing_primary_key", card_family="referential")
        self.assertEqual(card.card_family, "referential")


# ---------------------------------------------------------------------------
# Pass 6: severity / source_type / confidence sync
# ---------------------------------------------------------------------------

def _det_finding(entity, severity, confidence=1.0):
    """Finding helper. confidence=1.0 signals deterministic provenance to the sync pass."""
    f = _finding(entity, "missing_primary_key")
    f.severity = severity
    f.confidence = confidence
    return f


class TestSyncSeverityFromFindings(unittest.TestCase):

    def _card_defaults(self, fix_id, target, source_rule="missing_primary_key"):
        """Card with all LLM-default values."""
        return FixCard(
            fix_id=fix_id, title=fix_id, target=target,
            rationale="Fix needed.", sql="ALTER TABLE t ADD PRIMARY KEY (a, b);",
            source_rule=source_rule,
            severity="medium",       # LLM default
            source_type="ai",        # LLM default
            confidence=0.9,          # LLM default
        )

    def test_critical_finding_upgrades_card_severity(self):
        card = self._card_defaults("add-pk-campagne-contacts", "campagne_contacts")
        f = _det_finding("campagne_contacts", "critical", confidence=1.0)
        _sync_severity_from_findings([card], [f])
        self.assertEqual(card.severity, "critical")

    def test_high_finding_upgrades_medium_card(self):
        card = self._card_defaults("create-idx-groupe", "groupe.admin_id", "missing_index")
        f = _det_finding("groupe.admin_id", "high", confidence=1.0)
        _sync_severity_from_findings([card], [f])
        self.assertEqual(card.severity, "high")

    def test_medium_finding_does_not_downgrade_high_card(self):
        card = self._card_defaults("fix-a", "some_table")
        card.severity = "high"
        f = _det_finding("some_table", "medium")
        _sync_severity_from_findings([card], [f])
        self.assertEqual(card.severity, "high")  # never downgrade

    def test_deterministic_finding_fixes_source_type(self):
        card = self._card_defaults("add-pk-x", "some_table")
        f = _det_finding("some_table", "critical", confidence=1.0)
        _sync_severity_from_findings([card], [f])
        self.assertEqual(card.source_type, "deterministic")

    def test_sub_1_confidence_does_not_set_deterministic(self):
        """confidence < 1.0 → finding is LLM-generated → source_type stays 'ai'."""
        card = self._card_defaults("fix-b", "some_table")
        f = _det_finding("some_table", "high", confidence=0.85)
        _sync_severity_from_findings([card], [f])
        self.assertEqual(card.source_type, "ai")

    def test_confidence_upgraded_from_finding(self):
        card = self._card_defaults("add-pk-y", "some_table")
        self.assertAlmostEqual(card.confidence, 0.9)
        f = _det_finding("some_table", "critical", confidence=1.0)
        _sync_severity_from_findings([card], [f])
        self.assertAlmostEqual(card.confidence, 1.0)

    def test_higher_card_confidence_not_reduced(self):
        card = self._card_defaults("add-pk-z", "some_table")
        card.confidence = 0.95
        f = _det_finding("some_table", "high", confidence=0.85)
        _sync_severity_from_findings([card], [f])
        self.assertAlmostEqual(card.confidence, 0.95)

    def test_table_level_fallback_used(self):
        """Card targets 'campagne_contacts' (table-only). Finding entity also matches."""
        card = self._card_defaults("add-pk-cc", "campagne_contacts")
        f = _det_finding("campagne_contacts", "critical", confidence=1.0)
        _sync_severity_from_findings([card], [f])
        self.assertEqual(card.severity, "critical")

    def test_no_matching_finding_leaves_card_unchanged(self):
        card = self._card_defaults("fix-unknown", "nonexistent_table")
        original_sev = card.severity
        _sync_severity_from_findings([card], [])
        self.assertEqual(card.severity, original_sev)

    def test_run_1176e313_add_pk_cards(self):
        """Reproduce the exact severity/source_type/confidence failure from run 1176e313."""
        cards = [
            self._card_defaults("add-pk-campagne-contacts",  "campagne_contacts"),
            self._card_defaults("add-pk-expediteur-groupe",  "expediteur_groupe"),
            self._card_defaults("add-pk-campagne-tags",      "campagne_tags"),
            self._card_defaults("add-pk-user-contact-tags",  "user_contact_tags"),
        ]
        findings = [
            _det_finding("campagne_contacts",  "critical", 1.0),
            _det_finding("expediteur_groupe",  "critical", 1.0),
            _det_finding("campagne_tags",      "critical", 1.0),
            _det_finding("user_contact_tags",  "critical", 1.0),
        ]
        _sync_severity_from_findings(cards, findings)
        for card in cards:
            self.assertEqual(card.severity, "critical", f"{card.fix_id} should be critical")
            self.assertEqual(card.source_type, "deterministic", f"{card.fix_id} source_type")
            self.assertAlmostEqual(card.confidence, 1.0, msg=f"{card.fix_id} confidence")


if __name__ == "__main__":
    unittest.main()
