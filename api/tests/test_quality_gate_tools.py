import unittest

from agents.tools import enforce_quality_gate


class QualityGateToolTests(unittest.TestCase):
    def test_enforce_quality_gate_accepts_high_confidence_findings(self):
        findings = [
            {
                "id": "f1",
                "confidence_overall": 0.91,
                "evidence": {
                    "query": "SELECT 1",
                    "row_count": 3,
                    "sample": [{"id": 1}],
                },
            },
            {
                "id": "f2",
                "confidence": {"confidence_overall": 0.87},
                "evidence": {
                    "query": "SELECT 2",
                    "row_count": 2,
                    "sample": [{"id": 2}],
                },
            },
        ]

        result = enforce_quality_gate(
            findings=findings,
            min_confidence=0.8,
            min_evidence_rows=1,
            require_evidence_query=True,
        )

        self.assertEqual(len(result["accepted_findings"]), 2)
        self.assertEqual(len(result["blocked"]), 0)
        self.assertEqual(result["meta"]["status"], "ok")

    def test_enforce_quality_gate_blocks_low_confidence_or_missing_evidence(self):
        findings = [
            {
                "id": "low_conf",
                "confidence_overall": 0.4,
                "evidence": {
                    "query": "SELECT 1",
                    "row_count": 10,
                    "sample": [],
                },
            },
            {
                "id": "no_query",
                "confidence_overall": 0.9,
                "evidence": {
                    "query": "",
                    "row_count": 10,
                    "sample": [],
                },
            },
            {
                "id": "no_rows",
                "confidence_overall": 0.9,
                "evidence": {
                    "query": "SELECT 3",
                    "row_count": 0,
                    "sample": [],
                },
            },
        ]

        result = enforce_quality_gate(
            findings=findings,
            min_confidence=0.8,
            min_evidence_rows=1,
            require_evidence_query=True,
        )

        self.assertEqual(len(result["accepted_findings"]), 0)
        self.assertEqual(len(result["blocked"]), 3)
        self.assertEqual(result["meta"]["status"], "partial")

        blocked_by_id = {item["id"]: item["reason"] for item in result["blocked"]}
        self.assertIn("low_confidence", blocked_by_id["low_conf"])
        self.assertIn("missing_evidence_query", blocked_by_id["no_query"])
        self.assertIn("insufficient_evidence_rows", blocked_by_id["no_rows"])


if __name__ == "__main__":
    unittest.main()
