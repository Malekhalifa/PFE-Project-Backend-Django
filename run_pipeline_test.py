"""
One-shot pipeline test runner — calls analyze_postgres_database directly
without going through Celery. Output is saved to reports/ as usual.
"""
import os
import sys
import django

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
django.setup()

import json
from services.postgres_service import analyze_postgres_database

CONNECTION_STRING = "postgresql://m6l9k:malekkh@localhost:5432/easybulk"

if __name__ == "__main__":
    print("Starting pipeline run...", flush=True)
    result = analyze_postgres_database(CONNECTION_STRING, job_id="test-run-001", llm_model="gemini-2.5-flash")
    print("Pipeline complete.", flush=True)

    # Print a compact summary
    schema = result.get("schema_analysis", {}).get("json_report", {})
    statistical = result.get("statistical_analysis", {}).get("json_report", {})
    structural = result.get("structural_analysis", {}).get("json_report", {})
    relational = result.get("relational_analysis", {}).get("json_report", {})
    fixes = result.get("fixes", {}).get("fixes", [])

    def findings_count(report):
        return len(report.get("findings", []))

    print(f"\n=== RESULTS SUMMARY ===")
    print(f"Schema findings:      {findings_count(schema)}")
    print(f"Statistical findings: {findings_count(statistical)}")
    print(f"Structural findings:  {findings_count(structural)}")
    print(f"Relational findings:  {findings_count(relational)}")
    print(f"Fix cards:            {len(fixes)}")
    print(f"Confidence score:     {result.get('confidence_score')}")
    print(f"Data quality score:   {result.get('data_quality_score')}")

    print(f"\n=== RELATIONAL FINDINGS ===")
    for f in relational.get("findings", []):
        print(f"  [{f.get('severity','?')}] {f.get('entity', f.get('table','?'))} — {f.get('rule','?')} (conf={f.get('confidence','?')})")

    print(f"\n=== STRUCTURAL FINDINGS ===")
    for f in structural.get("findings", []):
        print(f"  [{f.get('severity','?')}] {f.get('entity', f.get('table','?'))} — {f.get('rule','?')} (conf={f.get('confidence','?')})")

    print(f"\n=== STATISTICAL FINDINGS ===")
    for f in statistical.get("findings", []):
        print(f"  [{f.get('severity','?')}] {f.get('entity', f.get('table','?'))} — {f.get('rule','?')} (conf={f.get('confidence','?')})")

    print(f"\n=== REPORT FILE ===")
    print(result.get("report_file_name", "(not saved)"))
