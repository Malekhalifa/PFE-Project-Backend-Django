import django.db.models.deletion
import django.utils.timezone
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('api', '0010_remove_job_file_fields'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name='LearningPattern',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('rule_family', models.CharField(max_length=128)),
                ('entity', models.CharField(max_length=255)),
                ('decision', models.CharField(
                    choices=[('accept', 'Accept'), ('reject', 'Reject')],
                    max_length=16,
                )),
                ('frequency', models.PositiveIntegerField(default=1)),
                ('last_seen', models.DateTimeField(auto_now=True)),
                ('user', models.ForeignKey(
                    db_column='user_id',
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name='learning_patterns',
                    to=settings.AUTH_USER_MODEL,
                )),
            ],
            options={
                'db_table': 'learning_patterns',
            },
        ),
        migrations.AlterUniqueTogether(
            name='learningpattern',
            unique_together={('user', 'rule_family', 'entity')},
        ),
    ]
