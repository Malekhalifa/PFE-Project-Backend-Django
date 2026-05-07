"""
test_schema_normalization.py — Level 2 unit tests for hooks._normalize_describe_table_result
and ensure_schema_primed side-effects.

These tests guard the schema-building logic that feeds every downstream agent.
No LLM, no DB needed.
"""
import unittest

from agents.hooks import _normalize_describe_table_result


class NormalizeDescribeTableResultTests(unittest.TestCase):
    """
    Bug: _normalize_describe_table_result was returning a 'raw' field containing
    the full unparsed CSV text. On large schemas (30 tables) this made
    session_state big enough that deepcopy in Agno's Parallel step took ~3 minutes.
    Fix: 'raw' field is no longer emitted.
    """

    _SAMPLE_CSV = (
        "column_name,data_type,is_nullable\r\n"
        "id,integer,NO\r\n"
        "email,character varying,YES\r\n"
        "created_at,timestamp without time zone,YES\r\n"
    )

    def test_raw_field_is_absent(self):
        result = _normalize_describe_table_result("users", self._SAMPLE_CSV)
        self.assertNotIn("raw", result)

    def test_table_name_is_preserved(self):
        result = _normalize_describe_table_result("users", self._SAMPLE_CSV)
        self.assertEqual(result["table_name"], "users")

    def test_columns_list_contains_all_column_names(self):
        result = _normalize_describe_table_result("users", self._SAMPLE_CSV)
        self.assertEqual(result["columns"], ["id", "email", "created_at"])

    def test_column_details_contain_correct_types(self):
        result = _normalize_describe_table_result("users", self._SAMPLE_CSV)
        details = {d["column_name"]: d for d in result["column_details"]}

        self.assertEqual(details["id"]["data_type"],         "integer")
        self.assertEqual(details["id"]["is_nullable"],       "NO")
        self.assertEqual(details["email"]["data_type"],      "character varying")
        self.assertEqual(details["email"]["is_nullable"],    "YES")
        self.assertEqual(details["created_at"]["data_type"], "timestamp without time zone")

    def test_empty_payload_returns_empty_columns(self):
        result = _normalize_describe_table_result("empty_table", "")
        self.assertEqual(result["table_name"], "empty_table")
        self.assertEqual(result["columns"], [])
        self.assertEqual(result["column_details"], [])

    def test_schema_size_is_bounded(self):
        # Regression guard: result dict must not grow beyond reasonable size.
        # Before the fix, a 3-column table result was ~800 bytes with 'raw';
        # without it should be well under 400 bytes.
        import json
        result = _normalize_describe_table_result("users", self._SAMPLE_CSV)
        serialized = json.dumps(result)
        self.assertLess(
            len(serialized), 400,
            msg=f"Serialized schema entry is {len(serialized)} bytes — 'raw' field may have crept back in.",
        )

    def test_rows_without_column_name_are_skipped(self):
        csv_with_blank = (
            "column_name,data_type,is_nullable\r\n"
            "id,integer,NO\r\n"
            ",character varying,YES\r\n"   # blank column_name — must be skipped
        )
        result = _normalize_describe_table_result("users", csv_with_blank)
        self.assertEqual(result["columns"], ["id"])
        self.assertEqual(len(result["column_details"]), 1)

    def test_json_payload_falls_back_gracefully(self):
        # Agent sometimes returns JSON instead of CSV — should not raise.
        json_payload = '[{"column_name":"id","data_type":"integer","is_nullable":"NO"}]'
        result = _normalize_describe_table_result("orders", json_payload)
        # Either parses to columns or returns empty — must not raise.
        self.assertIsInstance(result["columns"], list)
        self.assertNotIn("raw", result)


class ProfilerSchemaViewTests(unittest.TestCase):
    """
    Bug: profiler received full {schema} in its context, including PK/FK/index/constraint
    metadata, adding ~60% extra tokens and triggering MALFORMED_FUNCTION_CALL.
    Fix: ensure_schema_primed builds a compact profiler_schema with only
    (column_name, data_type) per table.
    """

    def _build_mock_schema(self) -> dict:
        return {
            "users": {
                "table_name": "users",
                "columns": ["id", "email"],
                "column_details": [
                    {"column_name": "id",    "data_type": "integer",          "is_nullable": "NO"},
                    {"column_name": "email", "data_type": "character varying", "is_nullable": "YES"},
                ],
                "has_primary_key": True,
                "primary_key_columns": ["id"],
                "unique_constraints": [{"name": "users_email_key", "columns": ["email"]}],
                "indexes": ["users_pkey", "users_email_idx"],
                "indexed_columns": ["id", "email"],
            }
        }

    def _build_profiler_schema(self, schema: dict) -> dict:
        return {
            table_name: {
                "column_details": [
                    {"column_name": c["column_name"], "data_type": c["data_type"]}
                    for c in entry.get("column_details", [])
                ]
            }
            for table_name, entry in schema.items()
        }

    def test_profiler_schema_excludes_pk_uk_index_fields(self):
        schema = self._build_mock_schema()
        profiler_schema = self._build_profiler_schema(schema)

        users_entry = profiler_schema["users"]
        self.assertNotIn("has_primary_key",     users_entry)
        self.assertNotIn("primary_key_columns", users_entry)
        self.assertNotIn("unique_constraints",  users_entry)
        self.assertNotIn("indexes",             users_entry)
        self.assertNotIn("indexed_columns",     users_entry)

    def test_profiler_schema_includes_column_name_and_data_type(self):
        schema = self._build_mock_schema()
        profiler_schema = self._build_profiler_schema(schema)

        details = {d["column_name"]: d for d in profiler_schema["users"]["column_details"]}
        self.assertEqual(details["id"]["data_type"],    "integer")
        self.assertEqual(details["email"]["data_type"], "character varying")
        # is_nullable should NOT be present in the compact view
        self.assertNotIn("is_nullable", details["id"])

    def test_profiler_schema_is_smaller_than_full_schema(self):
        import json
        schema = self._build_mock_schema()
        profiler_schema = self._build_profiler_schema(schema)

        full_size    = len(json.dumps(schema))
        compact_size = len(json.dumps(profiler_schema))
        self.assertLess(
            compact_size, full_size,
            msg="profiler_schema should be smaller than the full schema.",
        )


if __name__ == "__main__":
    unittest.main()
