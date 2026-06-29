import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("labels", "0005_remove_labelgroup_slug"),
    ]

    operations = [
        migrations.CreateModel(
            name="LabelBackfillJob",
            fields=[
                (
                    "id",
                    models.AutoField(
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
                            ("CI", "Canceling"),
                            ("CA", "Canceled"),
                            ("SU", "Success"),
                            ("FA", "Failure"),
                        ],
                        default="PE",
                        max_length=2,
                    ),
                ),
                ("total_reports", models.PositiveIntegerField(default=0)),
                ("processed_reports", models.PositiveIntegerField(default=0)),
                ("message", models.TextField(blank=True, default="")),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("started_at", models.DateTimeField(blank=True, null=True)),
                ("ended_at", models.DateTimeField(blank=True, null=True)),
                (
                    "label_group",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="backfill_jobs",
                        to="labels.labelgroup",
                    ),
                ),
            ],
            options={
                "ordering": ["-created_at"],
            },
        ),
    ]
