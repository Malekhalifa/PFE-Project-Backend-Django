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
