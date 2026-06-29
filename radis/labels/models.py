from django.conf import settings
from django.db import models
from django.urls import reverse
from procrastinate.contrib.django import app
from procrastinate.contrib.django.models import ProcrastinateJob

from radis.core.models import AnalysisJob, AnalysisTask
from radis.reports.models import Report


class LabelGroup(models.Model):
    id: int
    name = models.CharField(max_length=100, unique=True)
    gate_question = models.TextField()  # upfront Yes/No screening question for this group
    updated_at = models.DateTimeField(auto_now=True)  # drives gate stale detection

    labels: models.QuerySet["Label"]
    gate_answers: models.QuerySet["GateAnswer"]

    class Meta:
        ordering = ["name"]

    def __str__(self) -> str:
        return f"LabelGroup {self.name} [{self.pk}]"


class Label(models.Model):
    id: int
    group = models.ForeignKey(LabelGroup, on_delete=models.CASCADE, related_name="labels")
    name = models.CharField(max_length=100)  # the label string that surfaces (e.g. "pneumonia")
    description = models.TextField()  # definition sent to the LLM to classify this label
    active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)  # drives result stale detection

    results: models.QuerySet["LabelResult"]

    class Meta:
        constraints = [models.UniqueConstraint(fields=["name"], name="unique_label_name")]
        indexes = [models.Index(fields=["active"])]

    def __str__(self) -> str:
        return f"Label {self.name} [{self.pk}]"


class LabelResult(models.Model):
    class Value(models.TextChoices):
        PRESENT = "PRESENT", "Present"
        LIKELY = "LIKELY", "Likely"
        POSSIBLE = "POSSIBLE", "Possible"
        ABSENT = "ABSENT", "Absent"
        UNMENTIONED = "UNMENTIONED", "Unmentioned"

    # Buckets that attach the label to the report / search.
    SURFACING_VALUES = (Value.PRESENT, Value.LIKELY, Value.POSSIBLE)

    label_id: int
    report = models.ForeignKey(Report, on_delete=models.CASCADE, related_name="label_results")
    label = models.ForeignKey(Label, on_delete=models.CASCADE, related_name="results")
    value = models.CharField(max_length=11, choices=Value.choices)
    generated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["report", "label"], name="unique_result_per_report_label"
            ),
        ]
        indexes = [
            models.Index(fields=["label", "value"]),  # search facet lookups
            models.Index(fields=["report"]),  # report detail page render
        ]

    def __str__(self) -> str:
        return f"LabelResult {self.label_id}={self.value} [{self.pk}]"

    @property
    def is_stale(self) -> bool:
        """A result is stale when its label's definition changed after it was generated."""
        return self.generated_at < self.label.updated_at


class GateAnswer(models.Model):
    class Value(models.TextChoices):
        YES = "YES", "Yes"
        NO = "NO", "No"

    label_group_id: int
    report = models.ForeignKey(Report, on_delete=models.CASCADE, related_name="gate_answers")
    label_group = models.ForeignKey(
        LabelGroup, on_delete=models.CASCADE, related_name="gate_answers"
    )
    value = models.CharField(max_length=3, choices=Value.choices)
    generated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["report", "label_group"], name="unique_gate_answer_per_report_group"
            ),
        ]
        indexes = [models.Index(fields=["label_group", "value"])]

    def __str__(self) -> str:
        return f"GateAnswer {self.label_group_id}={self.value} [{self.pk}]"


class LabelingScanCheckpoint(models.Model):
    last_scanned_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        verbose_name = "Labeling scan checkpoint"
        constraints = [
            models.CheckConstraint(
                condition=models.Q(id=1), name="singleton_labeling_scan_checkpoint"
            ),
        ]

    def __str__(self) -> str:
        return f"LabelingScanCheckpoint (last_scanned_at={self.last_scanned_at})"

    def save(self, *args, **kwargs):
        self.pk = 1
        super().save(*args, **kwargs)


class LabelingJob(AnalysisJob):
    class Trigger(models.TextChoices):
        SCAN = "SCAN", "Periodic scan"
        MANUAL = "MANUAL", "Manual backfill"

    default_priority = settings.LABELING_JOB_PRIORITY
    urgent_priority = settings.LABELING_JOB_PRIORITY  # labeling is never urgent

    # Scan jobs have no human owner; override the non-nullable base FK to allow null.
    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.CASCADE,
        related_name="%(app_label)s_jobs",
    )
    queued_job_id: int | None
    queued_job = models.OneToOneField(
        ProcrastinateJob, null=True, on_delete=models.SET_NULL, related_name="+"
    )
    trigger = models.CharField(max_length=10, choices=Trigger.choices, default=Trigger.MANUAL)
    scan_from = models.DateTimeField(null=True, blank=True)

    tasks: models.QuerySet["LabelingTask"]

    ACTIVE_STATUSES = (
        AnalysisJob.Status.UNVERIFIED,
        AnalysisJob.Status.PREPARING,
        AnalysisJob.Status.PENDING,
        AnalysisJob.Status.IN_PROGRESS,
        AnalysisJob.Status.CANCELING,
    )

    class Meta:
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"LabelingJob [{self.pk}]"

    def get_absolute_url(self) -> str:
        return reverse("admin:labels_labelingjob_change", args=[self.pk])

    def delay(self) -> None:
        queued_job_id = app.configure_task(
            "radis.labels.tasks.process_labeling_job",
            allow_unknown=False,
            priority=self.default_priority,
        ).defer(job_id=self.pk)
        self.queued_job_id = queued_job_id
        self.save()

    # Labeling jobs never send a completion email in v1 (scan jobs have no owner, and there is
    # no labeling mail template). Defining this avoids the base class touching the undefined
    # `finished_mail_template` attribute.
    finished_mail_template = None

    def _send_job_finished_mail(self) -> None:
        return


class LabelingTask(AnalysisTask):
    job = models.ForeignKey(LabelingJob, on_delete=models.CASCADE, related_name="tasks")
    reports = models.ManyToManyField(Report, related_name="+")

    def get_absolute_url(self) -> str:
        return reverse("admin:labels_labelingtask_change", args=[self.pk])

    def delay(self) -> None:
        queued_job_id = app.configure_task(
            "radis.labels.tasks.process_labeling_task",
            allow_unknown=False,
            priority=self.job.default_priority,
        ).defer(task_id=self.pk)
        self.queued_job_id = queued_job_id
        self.save()
