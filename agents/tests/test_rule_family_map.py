from __future__ import annotations

import unittest

from agents.rule_family_map import classify_fix


class RuleFamilyMapTests(unittest.TestCase):
    def test_current_deterministic_rules_are_classified(self):
        cases = {
            "missing_pk": ("schema_design", "execute_ddl", "deterministic"),
            "null_ratio_high": ("nullability", "advisory", "deterministic"),
            "nullable_fk_column": ("referential", "human_decision", "deterministic"),
            "missing_index_fk_column": ("schema_design", "execute_ddl", "deterministic"),
        }
        for rule, expected in cases.items():
            with self.subTest(rule=rule):
                self.assertEqual(classify_fix(rule, "schema"), expected)

    def test_future_aliases_are_classified(self):
        self.assertEqual(classify_fix("null_rate_high", "profiler"), ("nullability", "advisory", "deterministic"))
        self.assertEqual(classify_fix("missing_fk_index", "schema"), ("schema_design", "execute_ddl", "deterministic"))

    def test_unknown_rule_defaults_to_investigation(self):
        self.assertEqual(classify_fix("brand_new_rule", "relational"), ("unknown", "investigate", "ai"))


if __name__ == "__main__":
    unittest.main()
