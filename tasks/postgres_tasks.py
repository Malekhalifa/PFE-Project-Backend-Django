import logging
import os
import time
from datetime import datetime, timedelta, timezone

from django.db import close_old_connections
from django.db.models import Q
from django.utils import timezone as dj_tz
from config.celery import app
from services.postgres_service import analyze_postgres_database
from services.postgres_connection import parse_postgres_dsn_info
from services.notification_service import (
    notify_admin_job_failed,
    notify_admin_job_stuck,
    notify_user_analysis_started,
    notify_user_analysis_results_ready,
    notify_user_job_failed,
)
from api.db import (
    get_job,
    get_analysis_result,
    update_job_status,
    update_job_celery_task_id,
    save_analysis_result,
    update_job_cleaning_report,
)
from api.models import Job, audit_log
from users.models import User

_logger = logging.getLogger(__name__)
_DEFAULT_POSTGRES_LLM_MODEL = os.environ.get('GEMINI_MODEL', 'gemini-2.0-flash')


def _safe_notify(func, *args):
    try:
        return func(*args)
    except Exception:
        _logger.exception('Notification call failed: %s', getattr(func, '__name__', str(func)))
        return None


def _get_job_user(job_id: str) -> User | None:
    job = get_job(job_id) or {}
    user_id = job.get('user_id')
    if not user_id:
        return None

    try:
        user = User.objects.filter(id=user_id).only('id', 'email', 'email_notifications_enabled').first()
    except Exception:
        _logger.exception('Failed to resolve user contact for job %s', job_id)
        return None

    return user


def _get_job_context(job_id: str) -> tuple[str | None, str | None]:
    """
    Return (db_name, analysis_date) extracted from the job row.
    Either can be None if not available. Used to enrich notification emails.
    """
    job = get_job(job_id) or {}
    db_name = None
    connection_string = job.get('connection_string')
    if connection_string:
        try:
            db_name = parse_postgres_dsn_info(connection_string).database
        except Exception:
            pass

    analysis_date = None
    raw_ts = job.get('created_at')
    if raw_ts:
        try:
            if hasattr(raw_ts, 'strftime'):
                analysis_date = raw_ts.strftime('%Y-%m-%d %H:%M UTC')
            else:
                from datetime import datetime as _dt
                parsed = _dt.fromisoformat(str(raw_ts).replace('Z', '+00:00'))
                analysis_date = parsed.strftime('%Y-%m-%d %H:%M UTC')
        except Exception:
            analysis_date = str(raw_ts)

    return db_name, analysis_date

@app.task(bind=True, max_retries=0, time_limit=1800) # 30 mins limit to give AI agents enough time
def analyze_postgres_task(
    self,
    job_id: str,
    connection_string: str,
    domain_rules: list = None,
    llm_model: str | None = None,
):
    """
    Run PostgreSQL agentic analysis asynchronously using the Multi-Agent orchestrator.

    Mirrors the lifecycle pattern of `analyze_csv_task`:
      1. Set job status to 'running'
      2. Trigger the AI agents via postgres_service
        3. Store result in PostgreSQL
      4. Set job status to 'completed' or 'failed'
    """
    # Reset stale DB connections inherited from the parent after fork
    close_old_connections()

    update_job_status(job_id, 'running')
    if str((get_job(job_id) or {}).get('status') or '').upper() == 'CANCELLED':
        audit_log(job_id, 'DB_ANALYSIS_CANCELLED_BEFORE_START', {'status_code': 200})
        update_job_celery_task_id(job_id, None)
        return

    job_payload = get_job(job_id) or {}
    resolved_llm_model = (
        (job_payload.get('llm_model') if isinstance(job_payload, dict) else None)
        or llm_model
        or _DEFAULT_POSTGRES_LLM_MODEL
    )
    resolved_llm_model = str(resolved_llm_model).strip() or _DEFAULT_POSTGRES_LLM_MODEL

    audit_log(job_id, 'DB_ANALYSIS_STARTED', {
        'target': 'PostgreSQL Agentic Workflow',
        'status_code': 102,
        'llm_model': resolved_llm_model,
    })

    job_user = _get_job_user(job_id)
    if job_user and bool(getattr(job_user, 'email_notifications_enabled', True)):
        _job_db_name, _job_date = _get_job_context(job_id)
        _safe_notify(notify_user_analysis_started, job_user.email, job_id, _job_db_name, _job_date)

    try:
        started_at = datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')
        t0 = time.perf_counter()

        # Execute the AI Multi-Agent orchestration (job_id enables real-time progress updates)
        raw_results = analyze_postgres_database(
            connection_string,
            custom_rules=domain_rules,
            job_id=job_id,
            llm_model=resolved_llm_model,
        )

        duration_ms = int((time.perf_counter() - t0) * 1000)
        completed_at = datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')

        # Attach standard metadata
        raw_results['meta'] = {
            'job_id': job_id,
            'analysis_type': 'postgres_agentic',
            'llm_model': resolved_llm_model,
            'started_at': started_at,
            'completed_at': completed_at,
            'duration_ms': duration_ms,
        }

        # Store the AI-generated reports in PostgreSQL under this job
        save_analysis_result(
            job_id=job_id,
            result=raw_results,
            analysis_type="postgres_agentic",
        )

        update_job_status(job_id, 'completed')
        update_job_celery_task_id(job_id, None)
        audit_log(job_id, 'DB_ANALYSIS_COMPLETED', {
            'analysis_type': 'postgres_agentic',
            'duration_ms': duration_ms,
            'status_code': 200,
        })

        refreshed_job_user = _get_job_user(job_id)
        if refreshed_job_user and bool(getattr(refreshed_job_user, 'email_notifications_enabled', True)):
            _done_db_name, _done_date = _get_job_context(job_id)
            _safe_notify(notify_user_analysis_results_ready, refreshed_job_user.email, job_id, _done_db_name, _done_date)

    except Exception as e:
        failed_at_step = 'unknown'
        try:
            failed_at_step = (get_job(job_id) or {}).get('status') or 'unknown'
        except Exception:
            _logger.exception('Failed to resolve failed pipeline step for job %s', job_id)

        update_job_status(job_id, 'failed')
        update_job_celery_task_id(job_id, None)
        audit_log(job_id, 'DB_ANALYSIS_FAILED', {
            'error': str(e),
            'status_code': 500,
        })
        _safe_notify(notify_admin_job_failed, job_id, failed_at_step, str(e))
        # Also notify the job owner
        failed_job_user = _get_job_user(job_id)
        if failed_job_user and bool(getattr(failed_job_user, 'email_notifications_enabled', True)):
            _fail_db_name, _fail_date = _get_job_context(job_id)
            _safe_notify(notify_user_job_failed, failed_job_user.email, job_id, _fail_db_name, _fail_date)
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
    1. Fetch the job from PostgreSQL (connection_string lives there).
    2. Set job status to "cleaning".
    3. Call run_cleaning_plan() from postgres_cleaning_service.
    4. Persist the execution report under CleaningResult.execution_log.
    5. Set final status to "cleaning_completed" (all/partial success)
       or "cleaning_failed" (all tables failed).
    """
    _cleaning_logger.info("execute_postgres_cleaning_task started for job %s", job_id)
    # Reset stale DB connections inherited from the parent after fork
    close_old_connections()

    update_job_status(job_id, 'cleaning')
    if str((get_job(job_id) or {}).get('status') or '').upper() == 'CANCELLED':
        audit_log(job_id, 'POSTGRES_CLEANING_CANCELLED_BEFORE_START', {'status_code': 200})
        update_job_celery_task_id(job_id, None)
        return

    audit_log(job_id, 'POSTGRES_CLEANING_STARTED', {'status_code': 102})

    try:
        # 1 – Retrieve the job document
        job = get_job(job_id)
        if not job:
            raise ValueError(f"Job {job_id!r} not found.")

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

        # 4 – Persist the report in the job row
        update_job_cleaning_report(job_id, report)

        # 5 – Final status
        final_status = (
            'cleaning_failed'
            if report.get('status') == 'failed'
            else 'cleaning_completed'
        )
        update_job_status(job_id, final_status)
        update_job_celery_task_id(job_id, None)
        audit_log(job_id, 'POSTGRES_CLEANING_COMPLETED', {
            'overall_status': report.get('status'),
            'tables_processed': len(report.get('tables', [])),
            'status_code': 200,
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
        update_job_celery_task_id(job_id, None)
        audit_log(job_id, 'POSTGRES_CLEANING_FAILED', {
            'error': str(exc),
            'status_code': 500,
        })
        # Do NOT re-raise — Celery max_retries=0 means a raise would permanently
        # fail the task and obscure the real error from the audit log.


@app.task(bind=True)
def check_stuck_jobs(self):
    """
    Periodic monitor that alerts admins when jobs stay in a running phase too long.

    Criteria:
      - status == 'running' OR status starts with 'running_'
      - updated_at older than 10 minutes
    """
    close_old_connections()

    now = dj_tz.now()
    stale_before = now - timedelta(minutes=10)

    stuck_jobs = Job.objects.filter(
        (Q(status='running') | Q(status__startswith='running_')),
        updated_at__lt=stale_before,
    ).only('id', 'status', 'updated_at')

    alert_count = 0
    for job in stuck_jobs:
        duration_minutes = max(10, int((now - job.updated_at).total_seconds() // 60))
        _safe_notify(notify_admin_job_stuck, job.id, job.status, duration_minutes)
        alert_count += 1

    if alert_count:
        _logger.warning('check_stuck_jobs sent %s alert(s)', alert_count)

    return {'stuck_jobs_alerted': alert_count}
