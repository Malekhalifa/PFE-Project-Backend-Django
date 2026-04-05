import logging
import time
from datetime import datetime, timezone

from config.celery import app
from services.postgres_service import analyze_postgres_database
from api.mongo import (
    get_db,
    get_job,
    get_analysis_result,
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


# ─────────────────────────────────────────────────────────────────────────────
# PostgreSQL Cleaning Plan Executor
# ─────────────────────────────────────────────────────────────────────────────

from services.postgres_cleaning_service import run_cleaning_plan

_cleaning_logger = logging.getLogger('qualitylab.cleaning')


@app.task(bind=True, max_retries=0, time_limit=1800)
def execute_postgres_cleaning_task(self, job_id: str):
    """
    Execute a legacy structured cleaning plan against the user's PostgreSQL database.

    Note: the current agent pipeline produces human-reviewed `fixes`, not a
    top-level `cleaning_plan`. This task is retained only for compatibility
    with older analysis results that still contain a structured plan.

    Lifecycle
    ---------
    1. Fetch the job from MongoDB (connection_string lives there).
    2. Set job status to "cleaning".
    3. Call run_cleaning_plan() from postgres_cleaning_service.
    4. Persist the execution report under job["cleaning_report"] in MongoDB.
    5. Set final status to "cleaning_completed" (all/partial success)
       or "cleaning_failed" (all tables failed).
    """
    _cleaning_logger.info("execute_postgres_cleaning_task started for job %s", job_id)
    update_job_status(job_id, 'cleaning')
    audit_log(job_id, 'POSTGRES_CLEANING_STARTED', {})

    try:
        # 1 – Retrieve the job document
        job = get_job(job_id)
        if not job:
            raise ValueError(f"Job {job_id!r} not found in MongoDB.")

        connection_string = job.get('connection_string')
        if not connection_string:
            raise ValueError(
                f"Job {job_id!r} has no connection_string. "
                "Was the job created with AnalyzePostgresView?"
            )

        # 2 – Retrieve the legacy cleaning plan from the analysis result
        analysis = get_analysis_result(job_id)
        result = (analysis or {}).get('result', {})
        plan = result.get('cleaning_plan')
        if not plan:
            if result.get('fixes'):
                raise ValueError(
                    f"Job {job_id!r} contains human-reviewed fix cards, not a legacy "
                    "cleaning_plan. Apply them via /api/execute-fix/<job_id>/."
                )
            raise ValueError(f"Job {job_id!r} has no cleaning_plan in analysis result.")

        # 3 – Execute
        report = run_cleaning_plan(connection_string, plan)

        # 4 – Persist the report in the job document
        now = datetime.now(timezone.utc)
        get_db().jobs.update_one(
            {'_id': job_id},
            {'$set': {
                'cleaning_report': report,
                'updated_at': now,
            }},
        )

        # 5 – Final status
        final_status = (
            'cleaning_failed'
            if report.get('status') == 'failed'
            else 'cleaning_completed'
        )
        update_job_status(job_id, final_status)
        audit_log(job_id, 'POSTGRES_CLEANING_COMPLETED', {
            'overall_status': report.get('status'),
            'tables_processed': len(report.get('tables', [])),
        })
        _cleaning_logger.info(
            "execute_postgres_cleaning_task finished for job %s — %s",
            job_id,
            final_status,
        )

    except Exception as exc:
        _cleaning_logger.error(
            "execute_postgres_cleaning_task failed for job %s: %s",
            job_id,
            exc,
            exc_info=True,
        )
        update_job_status(job_id, 'cleaning_failed')
        audit_log(job_id, 'POSTGRES_CLEANING_FAILED', {'error': str(exc)})
        # Do NOT re-raise — Celery max_retries=0 means a raise would permanently
        # fail the task and obscure the real error from the audit log.
