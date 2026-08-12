from django.db import migrations

from adit_radis_shared.common.utils.migration_utils import procrastinate_on_delete_sql


class Migration(migrations.Migration):
    dependencies = [
        ("subscriptions", "0011_filter_questions_and_extraction_results"),
        ("procrastinate", "0028_add_cancel_states"),
    ]

    operations = [
        migrations.RunSQL(
            sql=procrastinate_on_delete_sql("subscriptions", "subscriptionjob"),
            reverse_sql=procrastinate_on_delete_sql(
                "subscriptions", "subscriptionjob", reverse=True
            ),
        ),
        migrations.RunSQL(
            sql=procrastinate_on_delete_sql("subscriptions", "subscriptiontask"),
            reverse_sql=procrastinate_on_delete_sql(
                "subscriptions", "subscriptiontask", reverse=True
            ),
        ),
    ]
