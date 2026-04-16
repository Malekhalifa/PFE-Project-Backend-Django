"""
Celery tasks for CSV analysis and cleaning.

These tasks replace the Node.js -> Python HTTP delegation pattern.
Instead of calling a separate FastAPI service, we invoke the analysis
and cleaning functions directly within the Celery worker process.

Each task:
  1. Updates job status in MongoDB
  2. Calls the corresponding service function
  3. Stores the result in MongoDB
  4. Updates final job status
  5. Writes audit log entries
"""
import os
import logging
import time
from datetime import datetime, timezone

from django.db import close_old_connections
from config.celery import app
from services.analysis_service import (
    analyze_csv,
    analyze_csv_streaming,
    to_native,
)
from services.cleaning_service import clean_csv
from api.db import (
    get_job,
    update_job_status,
    update_job_celery_task_id,
    update_job_cleaned_path,
    save_analysis_result,
    save_cleaning_result,
)
from api.models import audit_log
from users.models import User
from services.notification_service import notify_user_cleaning_done


logger = logging.getLogger(__name__)


def _safe_notify(func, *args):
    try:
        return func(*args)
    except Exception:
        logger.exception('Notification call failed: %s', getattr(func, '__name__', str(func)))
        return None


def _get_job_user(job_id: str) -> User | None:
    job = get_job(job_id) or {}
    user_id = job.get('user_id')
    if not user_id:
        return None
    try:
        user = User.objects.filter(id=user_id).only('id', 'email', 'email_notifications_enabled').first()
    except Exception:
        logger.exception('Failed to resolve user email for job %s', job_id)
        return None
    return user


LARGE_FILE_THRESHOLD_BYTES = 100 * 1024 * 1024  # 100 MB


@app.task(bind=True, max_retries=0, time_limit=600)
def analyze_csv_task(self, job_id, file_path, mode, options=None):
    """
    Run CSV analysis asynchronously.

    Mirrors the Node.js analysisService.runAnalysis() flow:
      1. Set job status to 'running'
      2. Choose in-memory or streaming based on mode / file size
      3. Store result in MongoDB
      4. Set job status to 'completed' or 'failed'
    """
    # Reset DB connections inherited from the parent process after fork.
    # Without this, Django ORM calls silently do nothing in Celery fork workers.
    close_old_connections()

    update_job_status(job_id, 'running')
    if str((get_job(job_id) or {}).get('status') or '').upper() == 'CANCELLED':
        audit_log(job_id, 'ANALYSIS_CANCELLED_BEFORE_START', {'status_code': 200})
        update_job_celery_task_id(job_id, None)
        return

    audit_log(job_id, 'ANALYSIS_STARTED', {'status_code': 102})

    try:
        if not os.path.exists(file_path):
            raise FileNotFoundError(f'File not found: {file_path}')

        file_size = os.path.getsize(file_path)
        started_at = datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')
        t0 = time.perf_counter()

        # Choose analysis strategy
        if mode == 'large' or file_size > LARGE_FILE_THRESHOLD_BYTES:
            analysis_type = 'streaming'
            raw_results = analyze_csv_streaming(file_path)
        else:
            analysis_type = 'in-memory'
            with open(file_path, 'rb') as f:
                content = f.read()
            raw_results = analyze_csv(content)

        duration_ms = int((time.perf_counter() - t0) * 1000)
        completed_at = datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')

        result = to_native(raw_results)

        # Attach metadata (matches Python FastAPI worker's meta format)
        result['meta'] = {
            'job_id': job_id,
            'file_path': file_path,
            'file_size_bytes': file_size,
            'mode': mode,
            'analysis_type': analysis_type,
            'started_at': started_at,
            'completed_at': completed_at,
            'duration_ms': duration_ms,
        }

        # Store in MongoDB
        save_analysis_result(
            job_id=job_id,
            result=result,
            analysis_type=analysis_type,
        )

        update_job_status(job_id, 'completed')
        update_job_celery_task_id(job_id, None)
        audit_log(job_id, 'ANALYSIS_COMPLETED', {
            'analysis_type': analysis_type,
            'duration_ms': duration_ms,
            'status_code': 200,
        })

    except Exception as e:
        update_job_status(job_id, 'failed')
        update_job_celery_task_id(job_id, None)
        audit_log(job_id, 'ANALYSIS_FAILED', {
            'error': str(e),
            'status_code': 500,
        })
        raise


@app.task(bind=True, max_retries=0, time_limit=600)
def clean_csv_task(self, job_id, file_path, mode='auto', rules=None, options=None):
    """
    Run CSV cleaning asynchronously.

    Mirrors the Node.js cleaningService.runCleaning() flow:
      1. Set job status to 'cleaning'
      2. Call clean_csv() service
      3. Store result in MongoDB
      4. Update job with cleaned file path
      5. Set job status to 'cleaned' or 'failed'
    """
    # Reset DB connections inherited from the parent process after fork.
    close_old_connections()

    update_job_status(job_id, 'cleaning')
    if str((get_job(job_id) or {}).get('status') or '').upper() == 'CANCELLED':
        audit_log(job_id, 'CLEANING_CANCELLED_BEFORE_START', {'status_code': 200})
        update_job_celery_task_id(job_id, None)
        return

    audit_log(job_id, 'CLEANING_STARTED', {
        'mode': mode,
        'status_code': 102,
    })

    try:
        result = clean_csv(
            file_path=file_path,
            job_id=job_id,
            mode=mode,
            rules=rules,
            options=options,
        )

        # Store in MongoDB
        save_cleaning_result(
            job_id=job_id,
            mode=result['mode'],
            rules_applied=result['rules_applied'],
            summary=result['summary'],
        )

        # Update job with cleaned file path
        update_job_cleaned_path(job_id, result['cleaned_file_path'])
        update_job_status(job_id, 'cleaned')
        update_job_celery_task_id(job_id, None)
        job_user = _get_job_user(job_id)
        if job_user and bool(getattr(job_user, 'email_notifications_enabled', True)):
            _safe_notify(notify_user_cleaning_done, job_user.email, job_id)

        # Audit log each rule applied
        for r in result['rules_applied']:
            audit_log(job_id, 'RULE_APPLIED', r)

        audit_log(job_id, 'CLEANING_COMPLETED', {
            'cleaned_file_path': result['cleaned_file_path'],
            'rows_removed': result['summary']['rows_removed'],
            'status_code': 200,
        })

    except Exception as e:
        # On failure, revert to 'completed' status (matches Node.js behavior)
        update_job_status(job_id, 'completed')
        update_job_celery_task_id(job_id, None)
        audit_log(job_id, 'ERROR', {
            'phase': 'cleaning',
            'error': str(e),
            'status_code': 500,
        })
        raise
