import base64
import hashlib

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


ENCRYPTED_PREFIX = 'enc:v1:'


def _build_fernet():
    from cryptography.fernet import Fernet

    configured_key = str(getattr(settings, 'JOB_CONNECTION_STRING_ENCRYPTION_KEY', '') or '').strip()
    key_source = (configured_key or settings.SECRET_KEY).encode('utf-8')

    try:
        decoded = base64.urlsafe_b64decode(key_source)
        if len(decoded) == 32:
            fernet_key = key_source
        else:
            raise ValueError('Invalid Fernet key length')
    except Exception:
        fernet_key = base64.urlsafe_b64encode(hashlib.sha256(key_source).digest())

    return Fernet(fernet_key)


def encrypt_existing_job_connection_strings(apps, schema_editor):
    job_model = apps.get_model('api', 'Job')
    fernet = _build_fernet()

    queryset = job_model.objects.exclude(connection_string__isnull=True).exclude(connection_string='')
    for job in queryset.iterator():
        value = job.connection_string
        if not isinstance(value, str):
            continue
        if value.startswith(ENCRYPTED_PREFIX):
            continue

        encrypted = fernet.encrypt(value.encode('utf-8')).decode('utf-8')
        job_model.objects.filter(pk=job.pk).update(connection_string=f'{ENCRYPTED_PREFIX}{encrypted}')


class Migration(migrations.Migration):

    dependencies = [
        ('users', '0006_user_email_notifications_enabled'),
        ('api', '0002_job_cleaningresult_chatmessage_analysisresult_and_more'),
    ]

    operations = [
        migrations.AddField(
            model_name='auditlog',
            name='user',
            field=models.ForeignKey(
                blank=True,
                db_column='user_id',
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name='audit_logs',
                to=settings.AUTH_USER_MODEL,
            ),
        ),
        migrations.RunPython(
            code=encrypt_existing_job_connection_strings,
            reverse_code=migrations.RunPython.noop,
        ),
    ]
