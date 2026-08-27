from django.db import migrations

from adit_radis_shared.common.utils.migration_utils import procrastinate_on_delete_sql


class Migration(migrations.Migration):
    dependencies = [
        ("labels", "0001_initial"),
        ("procrastinate", "0028_add_cancel_states"),
    ]

    operations = [
        migrations.RunSQL(
            sql=procrastinate_on_delete_sql("labels", "labelingjob"),
            reverse_sql=procrastinate_on_delete_sql("labels", "labelingjob", reverse=True),
        ),
        migrations.RunSQL(
            sql=procrastinate_on_delete_sql("labels", "labelingtask"),
            reverse_sql=procrastinate_on_delete_sql("labels", "labelingtask", reverse=True),
        ),
    ]
