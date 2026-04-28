import os
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import django
from rest_framework import status

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
django.setup()

from api.views import ExecuteFixView


class ExecuteFixViewTests(unittest.TestCase):
    def _request(self, data):
        return SimpleNamespace(
            data=data,
            user=SimpleNamespace(id="user-1", role="user", email_verified=True),
        )

    @patch("api.views.validate_fix_queries")
    @patch("api.views.audit_log")
    @patch("api.views.QueryExecutorAgent")
    @patch("api.views.find_owned_job")
    def test_post_returns_agent_execution_log_on_success(
        self,
        mock_find_owned_job,
        mock_query_executor_cls,
        mock_audit_log,
        mock_validate_fix_queries,
    ):
        mock_find_owned_job.return_value = {
            "connection_string": "postgresql://user:pass@localhost:5432/qualitylab"
        }

        execution_log = {
            "status": "success",
            "query": "UPDATE users SET email = lower(email);",
            "queries": ["UPDATE users SET email = lower(email);"],
            "database_id": "localhost_5432_qualitylab",
            "executed_at": "2026-04-11T12:00:00Z",
            "duration_ms": 12,
            "rows_affected": 5,
            "transaction": "committed",
            "error": None,
            "query_logs": [
                {
                    "index": 1,
                    "query": "UPDATE users SET email = lower(email);",
                    "status": "success",
                    "duration_ms": 12,
                    "rows_affected": 5,
                    "error": None,
                }
            ],
        }
        mock_query_executor_cls.return_value.execute.return_value = execution_log

        request = self._request({"queries": ["UPDATE users SET email = lower(email);"]})
        response = ExecuteFixView().post(request, "job-1")

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data, execution_log)
        mock_validate_fix_queries.assert_called_once_with([
            "UPDATE users SET email = lower(email);"
        ])
        mock_query_executor_cls.return_value.execute.assert_called_once_with(
            queries=["UPDATE users SET email = lower(email);"],
            connection_string="postgresql://user:pass@localhost:5432/qualitylab",
        )
        mock_audit_log.assert_called_once()
        self.assertEqual(mock_audit_log.call_args[0][1], "FIX_EXECUTED")

    @patch("api.views.validate_fix_queries")
    @patch("api.views.audit_log")
    @patch("api.views.QueryExecutorAgent")
    @patch("api.views.find_owned_job")
    def test_post_returns_agent_execution_log_on_failure(
        self,
        mock_find_owned_job,
        mock_query_executor_cls,
        mock_audit_log,
        mock_validate_fix_queries,
    ):
        mock_find_owned_job.return_value = {
            "connection_string": "postgresql://user:pass@localhost:5432/qualitylab"
        }

        execution_log = {
            "status": "failure",
            "query": "BROKEN SQL",
            "queries": ["BROKEN SQL"],
            "database_id": "localhost_5432_qualitylab",
            "executed_at": "2026-04-11T12:00:00Z",
            "duration_ms": 4,
            "rows_affected": 0,
            "transaction": "rolled_back",
            "error": {
                "code": "42601",
                "message": "syntax error at or near \"BROKEN\"",
                "detail": None,
            },
            "query_logs": [
                {
                    "index": 1,
                    "query": "BROKEN SQL",
                    "status": "failure",
                    "duration_ms": 4,
                    "rows_affected": 0,
                    "error": {
                        "code": "42601",
                        "message": "syntax error at or near \"BROKEN\"",
                        "detail": None,
                    },
                }
            ],
        }
        mock_query_executor_cls.return_value.execute.return_value = execution_log

        request = self._request({"queries": ["BROKEN SQL"]})
        response = ExecuteFixView().post(request, "job-1")

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data, execution_log)
        mock_validate_fix_queries.assert_called_once_with(["BROKEN SQL"])
        mock_audit_log.assert_called_once()
        self.assertEqual(mock_audit_log.call_args[0][1], "FIX_EXECUTION_FAILED")

    @patch("api.views.QueryExecutorAgent")
    @patch("api.views.find_owned_job")
    def test_post_rejects_invalid_queries_payload(self, mock_find_owned_job, mock_query_executor_cls):
        mock_find_owned_job.return_value = {
            "connection_string": "postgresql://user:pass@localhost:5432/qualitylab"
        }

        request = self._request({"queries": []})
        response = ExecuteFixView().post(request, "job-1")

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("queries must be a non-empty list", response.data.get("detail", ""))
        mock_query_executor_cls.return_value.execute.assert_not_called()

    @patch("api.views.validate_fix_queries", side_effect=ValueError("Safety violation"))
    @patch("api.views.audit_log")
    @patch("api.views.QueryExecutorAgent")
    @patch("api.views.find_owned_job")
    def test_post_rejects_queries_blocked_by_safety_validation(
        self,
        mock_find_owned_job,
        mock_query_executor_cls,
        mock_audit_log,
        _mock_validate_fix_queries,
    ):
        mock_find_owned_job.return_value = {
            "connection_string": "postgresql://user:pass@localhost:5432/qualitylab"
        }

        request = self._request({"queries": ["DROP TABLE users;"]})
        response = ExecuteFixView().post(request, "job-1")

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("Safety violation", response.data.get("detail", ""))
        mock_query_executor_cls.return_value.execute.assert_not_called()
        self.assertEqual(mock_audit_log.call_args[0][1], "FIX_EXECUTION_REJECTED")


if __name__ == "__main__":
    unittest.main()
