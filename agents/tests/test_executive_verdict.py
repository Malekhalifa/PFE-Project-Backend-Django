"""
test_executive_verdict.py — verdict demotion based on migration risk.

The verdict the user sees should reflect both diagnosis quality (data_quality_score)
AND remediation safety (count of high-risk / blocked / destructive fix cards).
A clean diagnosis paired with destructive cards is NOT low risk in practice.
"""
from __future__ import annotations

import unittest

from agents.validation import _build_executive_summary


def _empty_report() -> dict:
    return {"findings": []}


def _summary(*, score: int = 90, cards: list[dict] | None = None, fixes: list[dict] | None = None) -> dict:
    return _build_executive_summary(
        schema_dict=_empty_report(),
        statistical_dict=_empty_report(),
        structural_dict=_empty_report(),
        relational_dict=_empty_report(),
        fix_dict={"findings": fixes or [], "cards": cards or []},
        data_quality_score=score,
        total_tables=10,
        profiler_table_count=10,
    )


class VerdictDemotionTests(unittest.TestCase):
    def test_clean_diagnosis_no_destructive_cards_stays_low_risk(self):
        result = _summary(
            score=85,
            cards=[
                {"sql": "ALTER TABLE t ADD PRIMARY KEY (id);", "risk_level": "medium"},
                {"sql": "CREATE INDEX IF NOT EXISTS idx ON t (c);", "risk_level": "medium"},
            ],
        )
        self.assertEqual(result["verdict"], "low_risk")
        self.assertEqual(result["migration_risk"]["score"], 0)

    def test_high_risk_destructive_cards_demote_low_to_medium(self):
        result = _summary(
            score=90,
            cards=[
                {
                    "sql": "DELETE FROM contacts WHERE country_code = 'X'; ALTER TABLE contacts ADD CONSTRAINT uk UNIQUE (country_code);",
                    "risk_level": "high",
                },
            ],
        )
        # 1 high-risk card (10) + 1 destructive (8) = 18 → at least medium
        self.assertEqual(result["verdict"], "medium_risk")
        self.assertGreater(result["migration_risk"]["score"], 0)

    def test_many_destructive_cards_demote_low_to_high(self):
        cards = [
            {"sql": "DELETE FROM t1 WHERE x;", "risk_level": "high"},
            {"sql": "DELETE FROM t2 WHERE y;", "risk_level": "high"},
            {"sql": "DROP CONSTRAINT c;",      "risk_level": "high"},
        ]
        result = _summary(score=90, cards=cards)
        # 3 high (30) + 3 destructive (24) = 54 → at least high_risk
        self.assertEqual(result["verdict"], "high_risk")

    def test_blocked_cards_alone_demote_low_to_medium(self):
        cards = [
            {"sql": "-- BLOCKED BY PREFLIGHT: duplicate values\nSELECT 'x';",
             "risk_level": "low"},
        ]
        result = _summary(score=95, cards=cards)
        self.assertEqual(result["verdict"], "medium_risk")
        self.assertEqual(result["migration_risk"]["blocked_cards"], 1)

    def test_already_critical_not_promoted(self):
        # data_quality 10 → critical. Destructive cards shouldn't drop it further.
        result = _summary(
            score=10,
            cards=[{"sql": "DELETE FROM t;", "risk_level": "high"}],
        )
        self.assertEqual(result["verdict"], "critical")

    def test_migration_risk_object_present_when_no_cards(self):
        result = _summary(score=80, cards=[])
        self.assertIn("migration_risk", result)
        self.assertEqual(result["migration_risk"]["score"], 0)


if __name__ == "__main__":
    unittest.main()
