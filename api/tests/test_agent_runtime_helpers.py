import importlib
import os
import unittest
from types import SimpleNamespace

import django

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
django.setup()

from agents.hooks import _normalize_describe_table_result, _normalize_table_list_result
from agents.reducer import ReduceSignals
from agents.schemas import Finding, ValidationReport
from agents.tool_factories import resolve_pg_tool_kwargs


class ToolFactoryHelperTests(unittest.TestCase):
    def test_resolve_pg_tool_kwargs_uses_session_state_dsn(self):
        kwargs = resolve_pg_tool_kwargs(
            session_state={
                "connection_string": "postgresql://user%40name:pa%24%24@db.example.com:5433/qualitylab",
                "table_schema": "analytics",
            }
        )

        self.assertEqual(
            kwargs,
            {
                "db_name": "qualitylab",
                "host": "db.example.com",
                "port": 5433,
                "user": "user@name",
                "password": "pa$$",
                "table_schema": "analytics",
            },
        )


class SchemaNormalizationTests(unittest.TestCase):
    def test_normalize_table_list_result_reads_csv_output(self):
        payload = "table_name\nusers\norders\n"

        self.assertEqual(_normalize_table_list_result(payload), ["users", "orders"])

    def test_normalize_describe_table_result_reads_csv_output(self):
        payload = (
            "column_name,data_type,is_nullable\n"
            "id,uuid,NO\n"
            "email,text,YES\n"
        )

        normalized = _normalize_describe_table_result("users", payload)

        self.assertEqual(normalized["table_name"], "users")
        self.assertEqual(normalized["columns"], ["id", "email"])
        self.assertEqual(
            normalized["column_details"],
            [
                {"column_name": "id", "data_type": "uuid", "is_nullable": "NO"},
                {"column_name": "email", "data_type": "text", "is_nullable": "YES"},
            ],
        )


class ReducerTests(unittest.TestCase):
    def test_reduce_signals_dedupes_ranks_and_caps_findings(self):
        duplicate_low = Finding(
            validator="column",
            entity="users.email",
            rule="invalid_domain",
            severity="medium",
            confidence=0.61,
            evidence={"invalid_ratio": 0.1},
            suggested_action="Normalize domains.",
        )
        duplicate_high = Finding(
            validator="column",
            entity="users.email",
            rule="invalid_domain",
            severity="medium",
            confidence=0.91,
            evidence={"invalid_ratio": 0.18},
            suggested_action="Normalize domains.",
        )
        critical = Finding(
            validator="crossrow",
            entity="orders.user_id",
            rule="orphaned_fk",
            severity="critical",
            confidence=0.74,
            evidence={"orphaned_rows": 8},
            suggested_action="Repair or remove orphaned rows.",
        )
        filtered = Finding(
            validator="row",
            entity="users.phone",
            rule="invalid_phone",
            severity="high",
            confidence=0.3,
            evidence={"invalid_rows": 2},
            suggested_action="Repair malformed phone values.",
        )

        reducer = ReduceSignals(min_confidence=0.4, top_n=2)
        session = SimpleNamespace(session_data={})

        class _FakeStepInput:
            workflow_session = session

            def get_step_content(self, step_name):
                reports = {
                    "column": ValidationReport(
                        validator="column",
                        findings=[duplicate_low, duplicate_high],
                    ),
                    "row": ValidationReport(validator="row", findings=[filtered]),
                    "crossrow": ValidationReport(validator="crossrow", findings=[critical]),
                }
                return reports.get(step_name)

        result = reducer(_FakeStepInput())
        findings = result.content.findings

        self.assertEqual(len(findings), 2)
        self.assertEqual(findings[0].entity, "orders.user_id")
        self.assertEqual(findings[1].entity, "users.email")
        self.assertEqual(findings[1].confidence, 0.91)
        self.assertNotIn("users.phone", [item.entity for item in findings])
        self.assertEqual(
            session.session_data["last_reduced_findings"]["findings"][0]["entity"],
            "orders.user_id",
        )


class ModuleImportSmokeTests(unittest.TestCase):
    def test_runtime_modules_import_successfully(self):
        for module_name in (
            "agents.validation",
            "agents.agent_os",
            "services.postgres_service",
            "api.views",
        ):
            with self.subTest(module=module_name):
                module = importlib.import_module(module_name)
                self.assertIsNotNone(module)


if __name__ == "__main__":
    unittest.main()
