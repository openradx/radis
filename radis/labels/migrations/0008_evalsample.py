import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("labels", "0007_refactor_to_question_set"),
        ("reports", "0013_alter_report_options"),
    ]

    operations = [
        migrations.CreateModel(
            name="EvalSample",
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
                ("name", models.CharField(max_length=200, unique=True)),
                ("description", models.TextField(blank=True, default="")),
                ("target_size", models.PositiveIntegerField()),
                ("seed_value", models.IntegerField(blank=True, null=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                (
                    "question_set",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="eval_samples",
                        to="labels.questionset",
                    ),
                ),
                (
                    "reports",
                    models.ManyToManyField(related_name="eval_samples", to="reports.report"),
                ),
            ],
            options={"ordering": ["-created_at"]},
        ),
    ]
