"""Hybrid-search schema additions on top of pgsearch.0001_initial:

- Rename the per-report search row from `ReportSearchVector` to
  `ReportSearchIndex` (now holds the FTS tsvector *and* the dense
  embedding; future trigram column would also live there).
- Update the reverse accessor on Report (`search_vector` → `search_index`).
- Install the pgvector extension.
- Add the `embedding vector(1024)` column and its HNSW index for cosine
  similarity search.

Squashed from the previously-separate `0002_hybrid_search` (extension +
embedding field + HNSW) and `0003_rename_search_index` (RenameModel +
AlterField) so that hybrid search ships as a single coherent migration
rather than three intermediate states no operator will ever see in
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
    ]
