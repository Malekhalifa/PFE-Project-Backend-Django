import unittest

from api.fix_safety import validate_fix_queries


class ValidateFixQueriesTests(unittest.TestCase):
    def test_allows_safe_fix_queries_against_original_table_names(self):
        queries = [
            "CREATE TABLE IF NOT EXISTS contacts_backup (LIKE contacts INCLUDING ALL);",
            "INSERT INTO contacts SELECT * FROM contacts_archive ON CONFLICT DO NOTHING;",
            "UPDATE contacts SET msisdn = '+21600000000' WHERE msisdn IS NULL;",
            "DELETE FROM contacts WHERE ctid NOT IN (SELECT MIN(ctid) FROM contacts GROUP BY msisdn);",
            "ALTER TABLE contacts ALTER COLUMN msisdn TYPE TEXT USING msisdn::text;",
        ]

        validate_fix_queries(queries)

    def test_rejects_multiple_statements_in_one_list_item(self):
        with self.assertRaisesRegex(ValueError, "exactly one SQL statement"):
            validate_fix_queries(
                [
                    "UPDATE contacts SET msisdn = '+21600000000'; DELETE FROM contacts;"
                ]
            )

    def test_rejects_unsupported_verbs(self):
        with self.assertRaisesRegex(ValueError, "Unsupported SQL verb"):
            validate_fix_queries(["SELECT * FROM contacts;"])

    def test_rejects_dangerous_statements(self):
        with self.assertRaisesRegex(ValueError, "dangerous SQL statements"):
            validate_fix_queries(["DROP TABLE contacts;"])

    def test_rejects_dangerous_keywords_inside_allowed_verb_family(self):
        with self.assertRaisesRegex(ValueError, "dangerous SQL statements"):
            validate_fix_queries(["ALTER SYSTEM SET work_mem = '64MB';"])


if __name__ == "__main__":
    unittest.main()
