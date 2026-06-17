import logging
import os
import time
from datetime import datetime, timezone
from pathlib import Path


from django.db import close_old_connections
from config.celery import app

from services.postgres_service import analyze_postgres_database
from services.postgres_connection import parse_postgres_dsn_info
from services.notification_service import (
    notify_admin_job_failed,
    notify_user_analysis_started,
    notify_user_analysis_results_ready,
    notify_user_job_failed,
)
from api.db import (
    get_job,
    update_job_status,
    update_job_celery_task_id,
    save_analysis_result,
    get_analysis_result,
)
from api.models import audit_log
from users.models import User

_logger = logging.getLogger(__name__)

_DEFAULT_POSTGRES_LLM_MODEL = os.environ.get('GEMINI_MODEL', 'gemini-2.5-flash')


def _set_job_status(job_id: str, status: str) -> None:
    """Atomic helper: update DB status AND push the same status over the
    Django Channels WebSocket so connected clients are notified in real time.

    The DB path serves polling fallbacks; the WS path serves clients that have
    a healthy WebSocket. Without the WS push, terminal statuses (``completed``,
    ``failed``) only reach clients that are still actively polling — clients
    whose polling shut off after WS proved healthy stay stuck on the last
    intermediate status.
    """
    update_job_status(job_id, status)
    try:
        from asgiref.sync import async_to_sync
        from channels.layers import get_channel_layer
        layer = get_channel_layer()
        if layer is not None:
            async_to_sync(layer.group_send)(
                f"job_{job_id}",
                {"type": "job.status", "status": status},
            )
    except Exception as exc:
        _logger.debug("celery task WS push skipped (status=%s): %s", status, exc)


def _set_job_error(job_id: str, error_detail: dict) -> None:
    """Push structured error detail over WebSocket to connected clients.

    Called before ``_set_job_status(job_id, 'failed')`` so the frontend
    receives the classified error payload before the terminal status.
    """
    try:
        from asgiref.sync import async_to_sync
        from channels.layers import get_channel_layer
        layer = get_channel_layer()
        if layer is not None:
            async_to_sync(layer.group_send)(
                f"job_{job_id}",
                {"type": "job.error", **error_detail},
            )
    except Exception as exc:
        _logger.debug("celery task error WS push skipped: %s", exc)

_WORKFLOW_DB_DIR = Path(__file__).resolve().parent.parent / "tmp"


def _cleanup_job_db(job_id: str) -> None:
    db_path = _WORKFLOW_DB_DIR / f"qualitylab_{job_id}.db"
    try:
        if db_path.exists():
            db_path.unlink()
    except Exception:
        _logger.warning("Failed to remove per-job SQLite file %s", db_path)

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

# Notice: We remove massive retries constraints / delay heuristics to match clean architecture
@app.task(bind=True, max_retries=2, time_limit=1800)
def analyze_postgres_task(
    self,
    job_id: str,
    connection_string: str,
    domain_rules: list = None,
    llm_model: str | None = None,
    baseline_job_id: str | None = None,
):
    """
    Run PostgreSQL agentic analysis asynchronously using the Multi-Agent v2 orchestrator.
    """
    close_old_connections()

    _set_job_status(job_id, 'running')
    _logger.info("pipeline_status: running job=%s source=celery_task_start", job_id)
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
        'target': 'PostgreSQL Agentic Workflow V2',
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

        # Collect prior findings for post-run comparison only — NOT passed to agents.
        # Passing them as context would bias the LLM to reproduce old findings even
        # if the underlying issue has been fixed.  The agents run fresh against the
        # sandbox; issues that were resolved will simply not appear in new results.
        prior_findings: list[dict] = []
        if baseline_job_id:
            baseline_result = get_analysis_result(baseline_job_id)
            if baseline_result and baseline_result.get('result'):
                res = baseline_result['result']
                raw_findings = (res.get('final_report') or {}).get('findings', [])
                prior_findings = [f for f in raw_findings if isinstance(f, dict)]

        # Fresh analysis — no historical_signals so agents work independently.
        raw_results = analyze_postgres_database(
            connection_string,
            custom_rules=domain_rules,
            job_id=job_id,
            llm_model=resolved_llm_model,
            historical_signals=None,
            user_id=str(job_user.id) if job_user else None,
        )

        duration_ms = int((time.perf_counter() - t0) * 1000)
        completed_at = datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')

        # Post-run: identify which prior findings are no longer detected (= fixed).
        # Match on (table, rule-family) to be resilient to minor LLM phrasing changes.
        resolved_findings: list[dict] = []
        if prior_findings:
            new_findings = (raw_results.get('final_report') or {}).get('findings', [])
            new_keys: set[tuple[str, str]] = set()
            for nf in new_findings:
                if not isinstance(nf, dict):
                    continue
                t = (nf.get('table') or '').strip().lower()
                r = (nf.get('rule') or nf.get('problem') or '').strip().lower().split('_')[0]
                new_keys.add((t, r))

            for pf in prior_findings:
                t = (pf.get('table') or '').strip().lower()
                r = (pf.get('rule') or pf.get('problem') or '').strip().lower().split('_')[0]
                if (t, r) not in new_keys:
                    resolved_findings.append(pf)

        raw_results['meta'] = {
            'job_id': job_id,
            'analysis_type': 'postgres_agentic',
            'llm_model': resolved_llm_model,
            'started_at': started_at,
            'completed_at': completed_at,
            'duration_ms': duration_ms,
            **(
                {
                    'baseline_job_id': baseline_job_id,
                    'prior_findings_count': len(prior_findings),
                    'resolved_findings_count': len(resolved_findings),
                    'resolved_findings': resolved_findings,
                }
                if baseline_job_id else {}
            ),
        }

        save_analysis_result(
            job_id=job_id,
            result=raw_results,
            analysis_type="postgres_agentic",
        )

        _set_job_status(job_id, 'completed')
        _logger.info("pipeline_status: completed job=%s source=celery_task_end", job_id)
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
        _logger.error('analyze_postgres_task failed for job %s: %s', job_id, e, exc_info=True)
        # Note: we bypass explicit manual custom backoffs because modern Celery/LLM retry logic applies correctly 

        failed_at_step = 'unknown'
        try:
            failed_at_step = (get_job(job_id) or {}).get('status') or 'unknown'
        except Exception:
            _logger.exception('Failed to resolve failed pipeline step for job %s', job_id)

        # Classify the error and push structured detail over WebSocket before
        # the generic 'failed' status so the frontend can show a specific message.
        try:
            from agents.gemini_errors import classify_gemini_error, gemini_error_detail_payload
            error_code, _user_msg = classify_gemini_error(str(e))
            error_detail = gemini_error_detail_payload(error_code, failed_at_step)
            _set_job_error(job_id, error_detail)
        except Exception:
            _logger.debug("Error classification skipped for job %s", job_id)

        _set_job_status(job_id, 'failed')
        _logger.info("pipeline_status: failed job=%s source=celery_task_exception", job_id)
        update_job_celery_task_id(job_id, None)
        audit_log(job_id, 'DB_ANALYSIS_FAILED', {
            'error': str(e),
            'status_code': 500,
        })
        _safe_notify(notify_admin_job_failed, job_id, failed_at_step, str(e))
        
        failed_job_user = _get_job_user(job_id)
        if failed_job_user and bool(getattr(failed_job_user, 'email_notifications_enabled', True)):
            _fail_db_name, _fail_date = _get_job_context(job_id)
            _safe_notify(notify_user_job_failed, failed_job_user.email, job_id, _fail_db_name, _fail_date)
        raise
    finally:
        _cleanup_job_db(job_id)


# ── Stuck-job detection (Celery beat every 5 min) ────────────────────────────

# Per-step time budgets in milliseconds.  Steps that consistently take longer
# than their budget trigger a stuck notification over the job WebSocket.
_STEP_TIME_BUDGETS_MS: dict[str, int] = {
    "running_schema": 5 * 60 * 1000,       # 5 min
    "running_profiler": 5 * 60 * 1000,      # 5 min
    "running_statistical": 10 * 60 * 1000,  # 10 min
    "running_structural": 10 * 60 * 1000,   # 10 min
    "running_relational": 10 * 60 * 1000,   # 10 min
    "running_reducer": 3 * 60 * 1000,       # 3 min
    "running_fix_planner": 15 * 60 * 1000,  # 15 min (soft warning at 5 min)
}


def _get_step_started_at(job_id: str, status: str) -> str | None:
    """Read the ISO timestamp when *status* began from Redis step-timing hash."""
    try:
        import os as _os
        import redis as _redis_mod
        _r = _redis_mod.from_url(
            _os.environ.get("REDIS_URL", "redis://localhost:6379/0"),
            decode_responses=True,
            socket_connect_timeout=1,
        )
        rkey = f"ql:step_timing:{job_id}"
        return _r.hget(rkey, status)
    except Exception:
        return None


def _find_stuck_jobs() -> list[tuple[str, str, int]]:
    """Scan active jobs and return [(job_id, status, stuck_ms)] for any step
    that has exceeded its time budget."""
    from api.models import Job as JobModel

    now = datetime.now(timezone.utc)
    stuck: list[tuple[str, str, int]] = []

    active_statuses = list(_STEP_TIME_BUDGETS_MS.keys())
    qs = JobModel.objects.filter(
        status__in=active_statuses,
    ).only("id", "status", "updated_at")[:100]

    for job in qs:
        status = job.status
        budget = _STEP_TIME_BUDGETS_MS.get(status)
        if budget is None:
            continue

        # Prefer the Redis step-start timestamp; fall back to job.updated_at.
        started_at_iso = _get_step_started_at(str(job.id), status)
        if started_at_iso:
            try:
                started = datetime.fromisoformat(
                    started_at_iso.replace("Z", "+00:00")
                )
            except Exception:
                started = job.updated_at
        else:
            started = job.updated_at

        if started is None:
            continue

        stuck_ms = int((now - started).total_seconds() * 1000)
        if stuck_ms >= budget:
            stuck.append((str(job.id), status, stuck_ms))

    return stuck


def _notify_stuck_job(job_id: str, status: str, stuck_ms: int) -> None:
    """Push a stuck notification over WebSocket with a user-facing message.

    Data Fix Planner gets a reassuring message at 5 min (soft warning);
    at 15 min it becomes a hard alert suggesting cancel+retry.
    """
    is_fix_planner = status == "running_fix_planner"

    if is_fix_planner and stuck_ms < 15 * 60 * 1000:
        user_message = (
            "The Data Fix Planner is taking longer than usual "
            "— this is normal for complex datasets."
        )
        notification_type = "stuck_warning"
    elif is_fix_planner:
        user_message = (
            "The Data Fix Planner appears to be stuck. "
            "You can wait or cancel and retry the analysis."
        )
        notification_type = "stuck_alert"
    else:
        user_message = (
            "The analysis is taking longer than expected. "
            "You can wait or cancel and retry."
        )
        notification_type = "stuck_alert"

    # Re-check: skip notification if job has already completed / failed.
    try:
        current = get_job(job_id) or {}
        cur_status = str(current.get("status") or "")
        if cur_status in ("completed", "failed", "cancelled", "CANCELLED"):
            return
    except Exception:
        pass

    try:
        from asgiref.sync import async_to_sync
        from channels.layers import get_channel_layer
        layer = get_channel_layer()
        if layer is not None:
            async_to_sync(layer.group_send)(
                f"job_{job_id}",
                {
                    "type": "job.stuck",
                    "status": status,
                    "stuck_duration_ms": stuck_ms,
                    "user_message": user_message,
                    "notification_type": notification_type,
                },
            )
    except Exception as exc:
        _logger.debug("stuck notification WS push skipped: %s", exc)

    _logger.warning(
        "stuck_notification: job=%s status=%s stuck_ms=%d type=%s",
        job_id,
        status,
        stuck_ms,
        notification_type,
    )


@app.task(bind=True, max_retries=0, time_limit=120)
def check_stuck_jobs(self):
    """Celery beat task: detect pipeline steps stuck beyond their budget.

    Runs every 5 minutes (configured in config/celery.py).  For each active
    job sitting on a running_* status longer than the step's budget, pushes
    a ``job.stuck`` WebSocket event so the frontend can notify the user.
    """
    close_old_connections()
    stuck = _find_stuck_jobs()
    for job_id, status, stuck_ms in stuck:
        _notify_stuck_job(job_id, status, stuck_ms)
    if stuck:
        _logger.info("check_stuck_jobs: %d stuck job(s) notified", len(stuck))
