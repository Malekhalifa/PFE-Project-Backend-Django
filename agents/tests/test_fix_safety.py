"""
test_fix_safety.py — validate_fix_sql_safety hook coverage.

Focus: DO $$ ... $$ anonymous PL/pgSQL blocks must be allowed even when they
appear AFTER another statement in the same SQL batch. Earlier code prefix-checked
the whole batch text, missing DO blocks that follow a DELETE / ALTER / etc.
"""
import unittest

from agno.exceptions import OutputCheckError
from agno.run.agent import RunOutput

from agents.hooks import validate_fix_sql_safety
from agents.schemas import FixCard, FixPlan


def _run_output_with_plan(plan: FixPlan) -> RunOutput:
    out = RunOutput()
    out.content = plan
    return out


class ValidateFixSqlSafetyTests(unittest.TestCase):
    def test_standalone_do_block_allowed(self):
        plan = FixPlan(cards=[FixCard(
            title="Add unique constraint if missing",
            target="prm_organization.code",
            rationale="Idempotent guard.",
            sql=(
                "DO $$ BEGIN IF NOT EXISTS ("
                "SELECT 1 FROM pg_constraint WHERE conname = 'uk_x'"
                ") THEN ALTER TABLE prm_organization ADD CONSTRAINT uk_x UNIQUE (code); END IF; END $$;"
            ),
        )])
        # Should not raise.
        validate_fix_sql_safety(_run_output_with_plan(plan))

    def test_delete_then_do_block_allowed_in_same_batch(self):
        # The exact failure mode from the prod log on 2026-05-09:
        #   DELETE FROM ...; DO $$ ... ALTER TABLE ... END IF; END $$;
        plan = FixPlan(cards=[FixCard(
            title="Dedupe and add unique constraint",
            target="prm_organization.code",
            rationale="Remove dups, then add UNIQUE.",
            sql=(
                "DELETE FROM prm_organization WHERE id IN ("
                "SELECT id FROM (SELECT id, ROW_NUMBER() OVER (PARTITION BY code ORDER BY id) as rn "
                "FROM prm_organization) t WHERE t.rn > 1); "
                "DO $$ BEGIN IF NOT EXISTS ("
                "SELECT 1 FROM pg_constraint WHERE conname = 'uk_prm_organization_code' "
                "AND conrelid = 'prm_organization'::regclass"
                ") THEN ALTER TABLE prm_organization ADD CONSTRAINT uk_prm_organization_code UNIQUE (code); "
                "END IF; END $$;"
            ),
        )])
        # Must NOT raise — the DO block follows a DELETE in the same batch.
        validate_fix_sql_safety(_run_output_with_plan(plan))

    def test_drop_table_still_blocked(self):
        plan = FixPlan(cards=[FixCard(
            title="Drop legacy table",
            target="legacy",
            rationale="cleanup",
            sql="DROP TABLE legacy;",
        )])
        with self.assertRaises(OutputCheckError):
            validate_fix_sql_safety(_run_output_with_plan(plan))


if __name__ == "__main__":
    unittest.main()
