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

# Explicitly import all files containing Celery tasks to avoid 'unregistered task' errors
app.conf.imports = ('tasks.postgres_tasks',)

existing_schedule = dict(getattr(app.conf, 'beat_schedule', {}) or {})
existing_schedule['check-stuck-jobs-every-5-minutes'] = {
	'task': 'tasks.postgres_tasks.check_stuck_jobs',
	'schedule': crontab(minute='*/5'),
}
app.conf.beat_schedule = existing_schedule
