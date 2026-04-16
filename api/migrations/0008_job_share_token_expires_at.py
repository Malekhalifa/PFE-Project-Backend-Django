from datetime import timedelta

from django.db import migrations, models
from django.utils import timezone


DEFAULT_SHARE_TOKEN_TTL_HOURS = 24


def backfill_share_token_expiry(apps, schema_editor):
    Job = apps.get_model('api', 'Job')
    expires_at = timezone.now() + timedelta(hours=DEFAULT_SHARE_TOKEN_TTL_HOURS)
    (
        Job.objects
        .filter(share_token__isnull=False, share_token_expires_at__isnull=True)
        .exclude(share_token='')
        .update(share_token_expires_at=expires_at)
    )


class Migration(migrations.Migration):

    dependencies = [
        ('api', '0007_remove_job_webhook_url_override'),
    ]

    operations = [
        migrations.AddField(
            model_name='job',
            name='share_token_expires_at',
            field=models.DateTimeField(blank=True, db_index=True, null=True),
        ),
        migrations.RunPython(
            code=backfill_share_token_expiry,
            reverse_code=migrations.RunPython.noop,
        ),
    ]
