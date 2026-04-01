import time
from datetime import datetime, timezone

from config.celery import app
from services.postgres_service import analyze_postgres_database
from api.mongo import (
    update_job_status,
    save_analysis_result,
)
from api.models import audit_log

@app.task(bind=True, max_retries=0, time_limit=1800) # 30 mins limit to give AI agents enough time
def analyze_postgres_task(self, job_id: str, connection_string: str, domain_rules: list = None):
    """
    Run PostgreSQL agentic analysis asynchronously using the Multi-Agent orchestrator.

    Mirrors the lifecycle pattern of `analyze_csv_task`:
      1. Set job status to 'running'
      2. Trigger the AI agents via postgres_service
      3. Store result in MongoDB
      4. Set job status to 'completed' or 'failed'
    """
    update_job_status(job_id, 'running')
    audit_log(job_id, 'DB_ANALYSIS_STARTED', {"target": "PostgreSQL Agentic Workflow"})

    try:
        started_at = datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')
        t0 = time.perf_counter()

        # Execute the AI Multi-Agent orchestration (job_id enables real-time progress updates)
        raw_results = analyze_postgres_database(connection_string, custom_rules=domain_rules, job_id=job_id)

        duration_ms = int((time.perf_counter() - t0) * 1000)
        completed_at = datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')

        # Attach standard metadata
        raw_results['meta'] = {
            'job_id': job_id,
            'analysis_type': 'postgres_agentic',
            'started_at': started_at,
            'completed_at': completed_at,
            'duration_ms': duration_ms,
        }

        # Store the AI-generated reports in MongoDB under this job
        save_analysis_result(
            job_id=job_id,
            result=raw_results,
            analysis_type="postgres_agentic",
        )

        update_job_status(job_id, 'completed')
        audit_log(job_id, 'DB_ANALYSIS_COMPLETED', {
            'analysis_type': 'postgres_agentic',
            'duration_ms': duration_ms,
        })

    except Exception as e:
        update_job_status(job_id, 'failed')
        audit_log(job_id, 'DB_ANALYSIS_FAILED', {'error': str(e)})
        raise
