"""
Celery application for QualityLab.

Handles async PostgreSQL analysis and fix execution tasks.
"""
import os
from celery import Celery
from celery.schedules import crontab

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')

app = Celery('qualitylab')
app.config_from_object('django.conf:settings', namespace='CELERY')
app.autodiscover_tasks(['tasks'])


def _env_int(name: str, default: int) -> int:
	try:
		return max(1, int(os.environ.get(name, str(default))))
	except Exception:
		return default


# Keep default worker burst low for LLM-backed tasks.
app.conf.worker_concurrency = _env_int('CELERY_WORKER_CONCURRENCY', 1)
app.conf.worker_prefetch_multiplier = _env_int('CELERY_WORKER_PREFETCH_MULTIPLIER', 1)

# Explicitly import all files containing Celery tasks to avoid 'unregistered task' errors
app.conf.imports = ('tasks.postgres_tasks',)

existing_schedule = dict(getattr(app.conf, 'beat_schedule', {}) or {})
existing_schedule['check-stuck-jobs-every-5-minutes'] = {
	'task': 'tasks.postgres_tasks.check_stuck_jobs',
	'schedule': crontab(minute='*/5'),
}
app.conf.beat_schedule = existing_schedule
