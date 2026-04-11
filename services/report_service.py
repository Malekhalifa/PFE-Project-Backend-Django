import json
from pathlib import Path

from django.conf import settings

from agents.reporting import safe_file_component


def _safe_report_timestamp(run_timestamp: str) -> str:
    # Keep the logical timestamp while making it filesystem-safe.
    return safe_file_component(run_timestamp.replace(":", "-"))


def save_final_pipeline_report(report: dict) -> dict:
    database_id = safe_file_component(str(report.get("database_id") or "unknown_db"))
    run_timestamp = str(report.get("run_timestamp") or "unknown_timestamp")
    safe_timestamp = _safe_report_timestamp(run_timestamp)

    filename = f"report_{database_id}_{safe_timestamp}.json"
    reports_dir = Path(settings.REPORTS_DIR)
    reports_dir.mkdir(parents=True, exist_ok=True)
    report_path = reports_dir / filename

    with report_path.open("w", encoding="utf-8") as fh:
        json.dump(report, fh, ensure_ascii=False, indent=2)

    return {
        "report_file_path": str(report_path),
        "report_file_name": filename,
        "report_timestamp": run_timestamp,
        "report_database_id": str(report.get("database_id") or "unknown_db"),
    }
