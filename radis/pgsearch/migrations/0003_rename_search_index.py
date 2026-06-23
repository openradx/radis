"""Rename `ReportSearchVector` → `ReportSearchIndex` and the reverse
accessor `Report.search_vector` → `Report.search_index`.

The model now holds the FTS tsvector AND the dense embedding (and likely
a trigram column in the future). Its name should reflect its role
("the per-report search-backing row") rather than one specific field."""
import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("pgsearch", "0002_hybrid_search"),
        ("reports", "0013_alter_report_options"),
    ]

    operations = [
        migrations.RenameModel(
            old_name="ReportSearchVector",
            new_name="ReportSearchIndex",
        ),
        migrations.AlterField(
            model_name="reportsearchindex",
            name="report",
            field=models.OneToOneField(
                on_delete=django.db.models.deletion.CASCADE,
                related_name="search_index",
                to="reports.report",
            ),
        ),
    ]
