"""Partial index on rows still missing an embedding.

The admin's pending-embedding count (`ReportSearchIndexAdmin._embedding_pipeline_stats`)
runs `WHERE embedding IS NULL` on every changelist request. The HNSW index on
`embedding` can't serve an IS NULL predicate, so without this the count was a
full sequential scan of the whole table (~1.7M rows in production) on every
page load and admin-action redirect.
"""
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("pgsearch", "0002_hybrid_search"),
    ]

    operations = [
        migrations.AddIndex(
            model_name="reportsearchindex",
            index=models.Index(
                fields=["id"],
                condition=models.Q(embedding__isnull=True),
                name="pgsearch_pending_embedding_idx",
            ),
        ),
    ]
