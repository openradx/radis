"""Hybrid-search schema additions on top of pgsearch.0001_initial:

- Rename the per-report search row from `ReportSearchVector` to
  `ReportSearchIndex` (now holds the FTS tsvector *and* the dense
  embedding; a future trigram column would also live there), including
  the reverse accessor on Report (`search_vector` → `search_index`) and
  the auto-named GIN index that follows the model name.
- Install the pgvector extension.
- Add the `embedding vector(1024)` column and its HNSW index for cosine
  similarity search.
- Add a partial index on rows still missing an embedding: the admin's
  pending-embedding count runs `WHERE embedding IS NULL` on every
  changelist request, and the HNSW index can't serve an IS NULL
  predicate — without this the count is a sequential scan over millions
  of rows.
- Add `EmbeddingBackfillRun`, the run-history row behind the admin's
  pipeline badge and the single-active-backfill guard.

Squashed from the intermediate branch migrations (schema, partial index,
index rename, backfill-run model) so hybrid search ships as one coherent
migration rather than several states no operator will ever see in
isolation.
"""

import django.db.models.deletion
import pgvector.django.indexes
import pgvector.django.vector
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("pgsearch", "0001_initial"),
        ("reports", "0013_alter_report_options"),
    ]

    operations = [
        migrations.RunSQL(
            sql="CREATE EXTENSION IF NOT EXISTS vector;",
            reverse_sql=migrations.RunSQL.noop,
        ),
        migrations.RenameModel(
            old_name="ReportSearchVector",
            new_name="ReportSearchIndex",
        ),
        migrations.AlterModelOptions(
            name="reportsearchindex",
            options={
                "verbose_name": "Report search index",
                "verbose_name_plural": "Report search indexes",
            },
        ),
        migrations.RenameIndex(
            model_name="reportsearchindex",
            new_name="pgsearch_re_search__b0f715_gin",
            old_name="pgsearch_re_search__a80d52_gin",
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
        migrations.AddField(
            model_name="reportsearchindex",
            name="embedding",
            field=pgvector.django.vector.VectorField(dimensions=1024, null=True),
        ),
        migrations.AddIndex(
            model_name="reportsearchindex",
            index=pgvector.django.indexes.HnswIndex(
                ef_construction=64,
                fields=["embedding"],
                m=16,
                name="pgsearch_embedding_hnsw",
                opclasses=["vector_cosine_ops"],
            ),
        ),
        migrations.AddIndex(
            model_name="reportsearchindex",
            index=models.Index(
                fields=["id"],
                condition=models.Q(embedding__isnull=True),
                name="pgsearch_pending_embedding_idx",
            ),
        ),
        migrations.CreateModel(
            name="EmbeddingBackfillRun",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                ("started_at", models.DateTimeField(auto_now_add=True)),
                ("finished_at", models.DateTimeField(blank=True, null=True)),
                ("cancelled_at", models.DateTimeField(blank=True, null=True)),
                ("total_reports", models.PositiveIntegerField()),
                ("processed_reports", models.PositiveIntegerField(default=0)),
                ("triggered_by", models.CharField(max_length=150)),
            ],
            options={
                "ordering": ["-started_at"],
            },
        ),
    ]
