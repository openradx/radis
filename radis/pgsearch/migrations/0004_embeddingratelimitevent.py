from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("pgsearch", "0003_pending_embedding_index"),
    ]

    operations = [
        migrations.CreateModel(
            name="EmbeddingRateLimitEvent",
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
                ("bucket", models.CharField(max_length=32)),
                ("sent_at", models.DateTimeField()),
                ("weight", models.PositiveIntegerField(default=1)),
            ],
            options={
                "indexes": [
                    models.Index(
                        fields=["bucket", "sent_at"],
                        name="pgsearch_ratelimit_bucket_idx",
                    )
                ],
            },
        ),
    ]
