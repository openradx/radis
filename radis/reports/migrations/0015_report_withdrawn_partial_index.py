from django.contrib.postgres.operations import AddIndexConcurrently
from django.db import migrations, models


class Migration(migrations.Migration):
    # CREATE INDEX CONCURRENTLY cannot run inside a transaction.
    atomic = False

    dependencies = [
        ("reports", "0014_report_withdrawal_fields"),
    ]

    operations = [
        AddIndexConcurrently(
            model_name="report",
            index=models.Index(
                fields=["withdrawn_at"],
                condition=models.Q(withdrawn_at__isnull=False),
                name="reports_report_withdrawn_idx",
            ),
        ),
    ]
