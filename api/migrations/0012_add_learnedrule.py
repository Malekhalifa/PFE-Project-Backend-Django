from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('api', '0011_add_learningpattern'),
    ]

    operations = [
        migrations.CreateModel(
            name='LearnedRule',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('database_id', models.CharField(db_index=True, max_length=255)),
                ('table_name', models.CharField(max_length=255)),
                ('rule_text', models.TextField()),
                ('rule_type', models.CharField(max_length=128)),
                ('consecutive_clean_runs', models.PositiveIntegerField(default=0)),
                ('last_violation_run_id', models.CharField(blank=True, default='', max_length=64)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
            ],
            options={
                'db_table': 'learned_rules',
            },
        ),
        migrations.AlterUniqueTogether(
            name='learnedrule',
            unique_together={('database_id', 'table_name', 'rule_text')},
        ),
        migrations.AddIndex(
            model_name='learnedrule',
            index=models.Index(
                fields=['database_id', 'consecutive_clean_runs'],
                name='learned_rul_databas_idx',
            ),
        ),
    ]
