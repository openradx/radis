from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ("procrastinate", "0001_initial"),
        ("extractions", "0002_procrastinate_on_delete"),
    ]

    operations = [
        migrations.CreateModel(
            name="ExtractionResultExport",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("status", models.CharField(choices=[("pending", "Pending"), ("processing", "Processing"), ("completed", "Completed"), ("failed", "Failed")], default="pending", max_length=20)),
                ("file", models.FileField(blank=True, null=True, upload_to="extraction_exports/")),
                ("row_count", models.PositiveIntegerField(blank=True, null=True)),
                ("error_message", models.TextField(blank=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("started_at", models.DateTimeField(blank=True, null=True)),
                ("finished_at", models.DateTimeField(blank=True, null=True)),
                ("job", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="result_exports", to="extractions.extractionjob")),
                ("queued_job", models.OneToOneField(null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="+", to="procrastinate.procrastinatejob")),
                ("requested_by", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="+", to=settings.AUTH_USER_MODEL)),
            ],
            options={
                "ordering": ["-created_at"],
            },
        ),
    ]
