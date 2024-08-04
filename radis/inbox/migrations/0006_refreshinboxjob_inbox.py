# Generated by Django 5.0.7 on 2024-08-04 13:18

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('inbox', '0005_rename_title_inbox_name_and_more'),
    ]

    operations = [
        migrations.AddField(
            model_name='refreshinboxjob',
            name='inbox',
            field=models.ForeignKey(default=None, on_delete=django.db.models.deletion.CASCADE, related_name='refresh_jobs', to='inbox.inbox'),
            preserve_default=False,
        ),
    ]
