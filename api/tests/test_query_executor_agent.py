import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from agents.query_executor_agent import QueryExecutorAgent


class _FakePgError(Exception):
    def __init__(self, message: str, code: str = "23505", detail: str = "duplicate key"):
        super().__init__(message)
        self.pgcode = code
        self.diag = SimpleNamespace(message_detail=detail)


class QueryExecutorAgentTests(unittest.TestCase):
    def setUp(self):
        self.agent = QueryExecutorAgent()
        self.connection_string = "postgresql://user:pass@localhost:5432/qualitylab"

    @patch("agents.query_executor_agent.psycopg2.connect")
    def test_execute_success_commits_and_returns_log(self, mock_connect):
        admin_connection = MagicMock()
        connection = MagicMock()
        cursor = MagicMock()
        connection.cursor.return_value = cursor
        mock_connect.side_effect = [admin_connection, connection]

        rowcounts = iter([0, 3, 2])

        def execute_side_effect(_query):
            cursor.rowcount = next(rowcounts)

        cursor.execute.side_effect = execute_side_effect

        queries = [
            "UPDATE users SET email = lower(email);",
            "DELETE FROM users WHERE email IS NULL;",
        ]

        result = self.agent.execute(queries=queries, connection_string=self.connection_string)

        self.assertEqual(result["status"], "success")
        self.assertEqual(result["transaction"], "committed")
        self.assertEqual(result["rows_affected"], 5)
        self.assertEqual(len(result["query_logs"]), 2)
        self.assertTrue(all(log["status"] == "success" for log in result["query_logs"]))
        self.assertIsNone(result["error"])

        admin_connection.close.assert_called_once()
        connection.commit.assert_called_once()
        connection.rollback.assert_not_called()
        cursor.close.assert_called_once()
        connection.close.assert_called_once()

    @patch("agents.query_executor_agent.psycopg2.connect")
    def test_execute_failure_rolls_back_and_returns_pg_error(self, mock_connect):
        admin_connection = MagicMock()
        connection = MagicMock()
        cursor = MagicMock()
        connection.cursor.return_value = cursor
        mock_connect.side_effect = [admin_connection, connection]

        rowcounts = iter([0, 4])

        def execute_side_effect(query):
            if "BROKEN" in query:
                raise _FakePgError(
                    "duplicate key value violates unique constraint",
                    code="23505",
                    detail="Key (id)=(1) already exists.",
                )
            cursor.rowcount = next(rowcounts)

        cursor.execute.side_effect = execute_side_effect

        queries = [
            "UPDATE users SET email = lower(email);",
            "BROKEN SQL",
        ]

        result = self.agent.execute(queries=queries, connection_string=self.connection_string)

        self.assertEqual(result["status"], "failure")
        self.assertEqual(result["transaction"], "rolled_back")
        self.assertEqual(result["rows_affected"], 0)
        self.assertEqual(result["error"]["code"], "23505")
        self.assertIn("duplicate key value", result["error"]["message"])
        self.assertEqual(result["error"]["detail"], "Key (id)=(1) already exists.")

        self.assertEqual(len(result["query_logs"]), 2)
        self.assertEqual(result["query_logs"][0]["status"], "success")
        self.assertEqual(result["query_logs"][1]["status"], "failure")

        admin_connection.close.assert_called_once()
        connection.rollback.assert_called_once()
        connection.commit.assert_not_called()
        cursor.close.assert_called_once()
        connection.close.assert_called_once()

    def test_execute_rejects_invalid_query_payload(self):
        with self.assertRaisesRegex(ValueError, "non-empty list"):
            self.agent.execute(queries=[], connection_string=self.connection_string)


if __name__ == "__main__":
    unittest.main()
