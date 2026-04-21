from django.db import migrations


class Migration(migrations.Migration):
    """
    Remove legacy file-based columns from the jobs table.
    These fields were meaningful when the app supported CSV uploads,
    but the project is now PostgreSQL-only and none of these columns
    are populated or referenced anymore.
    """

    dependencies = [
        ('api', '0009_remove_job_share_token_and_more'),
    ]

    operations = [
        migrations.RemoveField(
            model_name='job',
            name='file_name',
        ),
        migrations.RemoveField(
            model_name='job',
            name='file_path',
        ),
        migrations.RemoveField(
            model_name='job',
            name='cleaned_file_path',
        ),
    ]
