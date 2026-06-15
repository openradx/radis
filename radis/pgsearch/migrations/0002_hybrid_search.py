import pgvector.django.indexes
import pgvector.django.vector
from django.db import migrations


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
        migrations.AddField(
            model_name="reportsearchvector",
            name="embedding",
            field=pgvector.django.vector.VectorField(dimensions=1024, null=True),
        ),
        migrations.AddIndex(
            model_name="reportsearchvector",
            index=pgvector.django.indexes.HnswIndex(
                ef_construction=64,
                fields=["embedding"],
                m=16,
                name="pgsearch_embedding_hnsw",
                opclasses=["vector_cosine_ops"],
            ),
        ),
    ]
