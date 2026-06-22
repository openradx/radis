"""Refactor labels schema: LabelGroup/LabelQuestion/LabelChoice/ReportLabel/LabelBackfillJob
were renamed and reshaped into QuestionSet/Question/AnswerOption/Answer/BackfillJob plus a
new LabelingRun model that records each LLM exchange.

This migration is destructive on purpose: the labels feature is on an unmerged dev
branch and there is no production data to preserve. A name-preserving migration would
require synthesizing LabelingRun rows from existing ReportLabel rows under a default
mode, which buys us nothing here and clouds the new model's invariants.

After applying this migration, re-seed questions via the ``labels_seed`` command.
"""

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("labels", "0006_labelbackfilljob"),
        ("reports", "0013_alter_report_options"),
    ]

    operations = [
        # Drop the old shape.
        migrations.DeleteModel(name="LabelBackfillJob"),
        migrations.DeleteModel(name="ReportLabel"),
        migrations.DeleteModel(name="LabelChoice"),
        migrations.DeleteModel(name="LabelQuestion"),
        migrations.DeleteModel(name="LabelGroup"),
        # Create the new shape.
        migrations.CreateModel(
            name="QuestionSet",
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
                ("name", models.CharField(max_length=100)),
                ("description", models.TextField(blank=True, default="")),
                ("is_active", models.BooleanField(default=True)),
                ("order", models.PositiveSmallIntegerField(default=0)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("last_edited_at", models.DateTimeField(blank=True, null=True)),
            ],
            options={"ordering": ["order", "name"]},
        ),
        migrations.CreateModel(
            name="Question",
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
                ("label", models.CharField(max_length=200)),
                ("question", models.CharField(blank=True, default="", max_length=300)),
                ("is_active", models.BooleanField(default=True)),
                ("order", models.PositiveSmallIntegerField(default=0)),
                ("version", models.PositiveIntegerField(default=1)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "question_set",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="questions",
                        to="labels.questionset",
                    ),
                ),
            ],
            options={"ordering": ["order", "label"]},
        ),
        migrations.AddConstraint(
            model_name="question",
            constraint=models.UniqueConstraint(
                fields=("question_set", "label"),
                name="unique_question_label_per_set",
            ),
        ),
        migrations.CreateModel(
            name="AnswerOption",
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
                ("value", models.CharField(max_length=50)),
                ("label", models.CharField(max_length=100)),
                ("is_unknown", models.BooleanField(default=False)),
                ("order", models.PositiveSmallIntegerField(default=0)),
                (
                    "question",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="options",
                        to="labels.question",
                    ),
                ),
            ],
            options={"ordering": ["order", "label"]},
        ),
        migrations.AddConstraint(
            model_name="answeroption",
            constraint=models.UniqueConstraint(
                fields=("question", "value"),
                name="unique_answer_option_value_per_question",
            ),
        ),
        migrations.CreateModel(
            name="LabelingRun",
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
                    "mode",
                    models.CharField(
                        choices=[("DI", "Direct"), ("RE", "Reasoned")], max_length=2
                    ),
                ),
                (
                    "status",
                    models.CharField(
                        choices=[
                            ("PE", "Pending"),
                            ("IP", "In Progress"),
                            ("SU", "Success"),
                            ("FA", "Failure"),
                        ],
                        default="PE",
                        max_length=2,
                    ),
                ),
                ("model_name", models.CharField(blank=True, default="", max_length=200)),
                ("reasoning_text", models.TextField(blank=True, default="")),
                ("raw_response", models.JSONField(blank=True, null=True)),
                ("error_message", models.TextField(blank=True, default="")),
                ("prompt_tokens", models.PositiveIntegerField(blank=True, null=True)),
                ("completion_tokens", models.PositiveIntegerField(blank=True, null=True)),
                ("latency_ms", models.PositiveIntegerField(blank=True, null=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("completed_at", models.DateTimeField(blank=True, null=True)),
                (
                    "report",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="labeling_runs",
                        to="reports.report",
                    ),
                ),
                (
                    "question_set",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="labeling_runs",
                        to="labels.questionset",
                    ),
                ),
            ],
            options={"ordering": ["-created_at"]},
        ),
        migrations.AddIndex(
            model_name="labelingrun",
            index=models.Index(
                fields=["report", "question_set", "mode", "status"],
                name="labels_labe_report__344dc4_idx",
            ),
        ),
        migrations.AddIndex(
            model_name="labelingrun",
            index=models.Index(
                fields=["question_set", "mode", "status"],
                name="labels_labe_questio_c891ad_idx",
            ),
        ),
        migrations.CreateModel(
            name="Answer",
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
                ("question_version", models.PositiveIntegerField()),
                ("confidence", models.FloatField(blank=True, null=True)),
                ("rationale", models.TextField(blank=True, default="")),
                ("verified", models.BooleanField(default=False)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "run",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="answers",
                        to="labels.labelingrun",
                    ),
                ),
                (
                    "report",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="answers",
                        to="reports.report",
                    ),
                ),
                (
                    "question",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="answers",
                        to="labels.question",
                    ),
                ),
                (
                    "option",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="answers",
                        to="labels.answeroption",
                    ),
                ),
            ],
            options={"ordering": ["-created_at"]},
        ),
        migrations.AddConstraint(
            model_name="answer",
            constraint=models.UniqueConstraint(
                fields=("run", "question"),
                name="unique_answer_per_run_question",
            ),
        ),
        migrations.AddIndex(
            model_name="answer",
            index=models.Index(
                fields=["report", "question"],
                name="labels_answ_report__f3ccae_idx",
            ),
        ),
        migrations.AddIndex(
            model_name="answer",
            index=models.Index(
                fields=["question", "question_version"],
                name="labels_answ_questio_da32ee_idx",
            ),
        ),
        migrations.CreateModel(
            name="BackfillJob",
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
                    "question_set",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="backfill_jobs",
                        to="labels.questionset",
                    ),
                ),
            ],
            options={"ordering": ["-created_at"]},
        ),
    ]
