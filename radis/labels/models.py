from django.conf import settings
from django.db import models

from radis.core.models import AnalysisJob, AnalysisTask
from radis.reports.models import Report


class Question(models.Model):
    text = models.TextField()
    label = models.CharField(max_length=100)
    group = models.CharField(max_length=100)
    active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = [models.Index(fields=["active", "group"])]
        constraints = [
            models.UniqueConstraint(fields=["label"], name="unique_question_label"),
        ]

    def __str__(self) -> str:
        return self.label


class Answer(models.Model):
    class Value(models.TextChoices):
        YES = "YES", "Yes"
        NO = "NO", "No"
        MAYBE = "MAYBE", "Maybe"

    report = models.ForeignKey(
        Report, on_delete=models.CASCADE, related_name="answers"
    )
    question = models.ForeignKey(
        Question, on_delete=models.CASCADE, related_name="answers"
    )
    value = models.CharField(max_length=5, choices=Value.choices)
    generated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["report", "question"], name="unique_answer_per_report_question"
            ),
        ]
        indexes = [
            models.Index(fields=["question", "value"]),
            models.Index(fields=["report"]),
        ]


class LabelingJob(AnalysisJob):
    """Singleton backfill job. At most one row may be in an active status."""

    ACTIVE_STATUSES = (
        AnalysisJob.Status.UNVERIFIED,
        AnalysisJob.Status.PREPARING,
        AnalysisJob.Status.PENDING,
        AnalysisJob.Status.IN_PROGRESS,
        AnalysisJob.Status.CANCELING,
    )

    default_priority = settings.LABELING_BACKFILL_PRIORITY
    urgent_priority = settings.LABELING_BACKFILL_PRIORITY

    def delay(self) -> None:
        from procrastinate.contrib.django import app

        queued_job_id = app.configure_task(
            "radis.labels.tasks.process_labeling_job",
            allow_unknown=False,
            priority=self.default_priority,
        ).defer(job_id=self.pk)
        self.queued_job_id = queued_job_id
        self.save()


class LabelingTask(AnalysisTask):
    job = models.ForeignKey(
        LabelingJob, on_delete=models.CASCADE, related_name="tasks"
    )
    reports = models.ManyToManyField(Report, related_name="+")
