import unittest

from api.fix_safety import validate_fix_queries


class ValidateFixQueriesTests(unittest.TestCase):
    def test_allows_safe_fix_queries_against_clean_tables(self):
        queries = [
            "CREATE TABLE IF NOT EXISTS contacts_clean (LIKE contacts INCLUDING ALL);",
            "INSERT INTO contacts_clean SELECT * FROM contacts ON CONFLICT DO NOTHING;",
            "UPDATE contacts_clean SET msisdn = '+21600000000' WHERE msisdn IS NULL;",
            "DELETE FROM contacts_clean WHERE ctid NOT IN (SELECT MIN(ctid) FROM contacts_clean GROUP BY msisdn);",
            "ALTER TABLE contacts_clean ALTER COLUMN msisdn TYPE TEXT USING msisdn::text;",
        ]

        validate_fix_queries(queries)

    def test_rejects_updates_to_source_tables(self):
        with self.assertRaisesRegex(ValueError, "target only _clean tables"):
            validate_fix_queries([
                "UPDATE contacts SET msisdn = '+21600000000' WHERE msisdn IS NULL;"
            ])

    def test_rejects_create_table_without_clean_suffix(self):
        with self.assertRaisesRegex(ValueError, "target only _clean tables"):
            validate_fix_queries([
                "CREATE TABLE contacts_backup (LIKE contacts INCLUDING ALL);"
            ])

    def test_rejects_multiple_statements_in_one_list_item(self):
        with self.assertRaisesRegex(ValueError, "exactly one SQL statement"):
            validate_fix_queries([
                "UPDATE contacts_clean SET msisdn = '+21600000000'; DELETE FROM contacts_clean;"
            ])

    def test_rejects_unsupported_verbs(self):
        with self.assertRaisesRegex(ValueError, "Unsupported SQL verb"):
            validate_fix_queries([
                "SELECT * FROM contacts_clean;"
            ])


if __name__ == "__main__":
    unittest.main()
