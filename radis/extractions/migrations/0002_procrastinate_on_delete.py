# Generated by Django 5.1.6 on 2025-02-26 08:51

from django.db import migrations

from adit_radis_shared.common.utils.migration_utils import procrastinate_on_delete_sql

class Migration(migrations.Migration):

    dependencies = [
        ("extractions", "0001_initial"),
        ("procrastinate", "0028_add_cancel_states"),
    ]

    operations = [
        migrations.RunSQL(
            sql=procrastinate_on_delete_sql("extractions", "extractionjob"),
            reverse_sql=procrastinate_on_delete_sql("extractions", "extractionjob", reverse=True),
        ),
        migrations.RunSQL(
            sql=procrastinate_on_delete_sql("extractions", "extractiontask"),
            reverse_sql=procrastinate_on_delete_sql("extractions", "extractiontask", reverse=True),
        ),
    ]
