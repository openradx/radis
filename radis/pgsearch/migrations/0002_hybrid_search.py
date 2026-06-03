import django.db.models.deletion
import pgvector.django.indexes
import pgvector.django.vector
from django.conf import settings
from django.db import migrations, models


def create_system_user(apps, schema_editor):
    User = apps.get_model(*settings.AUTH_USER_MODEL.split("."))
    User.objects.get_or_create(
        username=settings.EMBEDDING_SYSTEM_USERNAME,
        defaults={"is_active": False, "password": "!"},
    )


class Migration(migrations.Migration):

    dependencies = [
        ("pgsearch", "0001_initial"),
        ("reports", "0013_alter_report_options"),
        ("procrastinate", "0041_post_retry_failed_job"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
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
        migrations.CreateModel(
            name="EmbeddingJob",
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
                (
                    "status",
                    models.CharField(
                        choices=[
                            ("UV", "Unverified"),
                            ("PR", "Preparing"),
                            ("PE", "Pending"),
                            ("IP", "In Progress"),
                            ("CI", "Canceling"),
                            ("CA", "Canceled"),
                            ("SU", "Success"),
                            ("WA", "Warning"),
                            ("FA", "Failure"),
                        ],
                        default="UV",
                        max_length=2,
                    ),
                ),
                ("urgent", models.BooleanField(default=False)),
                ("send_finished_mail", models.BooleanField(default=False)),
                ("message", models.TextField(blank=True, default="")),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("started_at", models.DateTimeField(blank=True, null=True)),
                ("ended_at", models.DateTimeField(blank=True, null=True)),
                (
                    "owner",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="%(app_label)s_jobs",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "queued_job",
                    models.OneToOneField(
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="+",
                        to="procrastinate.procrastinatejob",
                    ),
                ),
            ],
            options={
                "ordering": ["-created_at"],
            },
        ),
        migrations.CreateModel(
            name="EmbeddingTask",
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
                (
                    "status",
                    models.CharField(
                        choices=[
                            ("PE", "Pending"),
                            ("IP", "In Progress"),
                            ("CA", "Canceled"),
                            ("SU", "Success"),
                            ("WA", "Warning"),
                            ("FA", "Failure"),
                        ],
                        default="PE",
                        max_length=2,
                    ),
                ),
                ("attempts", models.PositiveSmallIntegerField(default=0)),
                ("message", models.TextField(blank=True, default="")),
                ("log", models.TextField(blank=True, default="")),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("started_at", models.DateTimeField(blank=True, null=True)),
                ("ended_at", models.DateTimeField(blank=True, null=True)),
                (
                    "job",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="tasks",
                        to="pgsearch.embeddingjob",
                    ),
                ),
                (
                    "queued_job",
                    models.OneToOneField(
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="+",
                        to="procrastinate.procrastinatejob",
                    ),
                ),
                (
                    "reports",
                    models.ManyToManyField(
                        related_name="embedding_tasks", to="reports.report"
                    ),
                ),
            ],
            options={
                "ordering": ("id",),
                "abstract": False,
            },
        ),
        migrations.RunPython(create_system_user, reverse_code=migrations.RunPython.noop),
    ]
