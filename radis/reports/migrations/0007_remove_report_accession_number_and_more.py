# Generated by Django 4.2.10 on 2024-02-23 22:40

from django.db import migrations, models
import django.utils.timezone
import radis.core.validators


class Migration(migrations.Migration):

    dependencies = [
        ('reports', '0006_report_language'),
    ]

    operations = [
        migrations.RemoveField(
            model_name='report',
            name='accession_number',
        ),
        migrations.RemoveField(
            model_name='report',
            name='series_instance_uid',
        ),
        migrations.RemoveField(
            model_name='report',
            name='sop_instance_uid',
        ),
        migrations.RemoveField(
            model_name='report',
            name='study_instance_uid',
        ),
        migrations.AddField(
            model_name='report',
            name='created_at',
            field=models.DateTimeField(auto_now_add=True, default=django.utils.timezone.now),
            preserve_default=False,
        ),
        migrations.AddField(
            model_name='report',
            name='metadata',
            field=models.JSONField(default=dict, validators=[radis.core.validators.validate_metadata]),
        ),
        migrations.AddField(
            model_name='report',
            name='updated_at',
            field=models.DateTimeField(auto_now=True),
        ),
    ]
