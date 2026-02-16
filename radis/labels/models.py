from __future__ import annotations

from typing import Callable

from django.db import models

from radis.reports.models import Report


class LabelBackfillJob(models.Model):
    class Status(models.TextChoices):
        PENDING = "PE", "Pending"
        IN_PROGRESS = "IP", "In Progress"
        CANCELING = "CI", "Canceling"
        CANCELED = "CA", "Canceled"
        SUCCESS = "SU", "Success"
        FAILURE = "FA", "Failure"

    label_group = models.ForeignKey(
        "LabelGroup", on_delete=models.CASCADE, related_name="backfill_jobs"
    )
    status = models.CharField(max_length=2, choices=Status.choices, default=Status.PENDING)
    get_status_display: Callable[[], str]
    total_reports = models.PositiveIntegerField(default=0)
    processed_reports = models.PositiveIntegerField(default=0)
    message = models.TextField(blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    started_at = models.DateTimeField(null=True, blank=True)
    ended_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"LabelBackfillJob [{self.pk}]"

    @property
    def is_cancelable(self) -> bool:
        return self.status in [
            self.Status.PENDING,
            self.Status.IN_PROGRESS,
        ]

    @property
    def is_active(self) -> bool:
        return self.status in [
            self.Status.PENDING,
            self.Status.IN_PROGRESS,
        ]

    @property
    def progress_percent(self) -> int:
        if self.total_reports == 0:
            return 0
        return min(int((self.processed_reports / self.total_reports) * 100), 100)


class LabelGroup(models.Model):
    id: int
    name = models.CharField(max_length=100)
    description = models.TextField(blank=True, default="")
    is_active = models.BooleanField(default=True)
    order = models.PositiveSmallIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    questions: models.QuerySet["LabelQuestion"]

    class Meta:
        ordering = ["order", "name"]

    def __str__(self) -> str:
        return f"LabelGroup {self.name} [{self.pk}]"


class LabelQuestion(models.Model):
    id: int
    group_id: int
    group = models.ForeignKey[LabelGroup](
        LabelGroup, on_delete=models.CASCADE, related_name="questions"
    )
    label = models.CharField(max_length=200)
    question = models.CharField(max_length=300, blank=True, default="")
    is_active = models.BooleanField(default=True)
    order = models.PositiveSmallIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    choices: models.QuerySet["LabelChoice"]
    get_name_display: Callable[[], str]

    class Meta:
        ordering = ["order", "label"]
        constraints = [
            models.UniqueConstraint(
                fields=["group", "label"],
                name="unique_label_question_label_per_group",
            )
        ]

    def __str__(self) -> str:
        return f"LabelQuestion {self.label} [{self.pk}]"

    def save(self, *args, **kwargs) -> None:
        if not self.question:
            self.question = self.label
        super().save(*args, **kwargs)


class LabelChoice(models.Model):
    id: int
    question = models.ForeignKey[LabelQuestion](
        LabelQuestion, on_delete=models.CASCADE, related_name="choices"
    )
    value = models.CharField(max_length=50)
    label = models.CharField(max_length=100)
    is_unknown = models.BooleanField(default=False)
    order = models.PositiveSmallIntegerField(default=0)

    get_label_display: Callable[[], str]

    class Meta:
        ordering = ["order", "label"]
        constraints = [
            models.UniqueConstraint(
                fields=["question", "value"],
                name="unique_label_choice_value_per_question",
            )
        ]

    def __str__(self) -> str:
        return f"LabelChoice {self.label} [{self.pk}]"


class ReportLabel(models.Model):
    report_id: int
    question_id: int
    report = models.ForeignKey[Report](Report, on_delete=models.CASCADE, related_name="labels")
    question = models.ForeignKey[LabelQuestion](
        LabelQuestion, on_delete=models.CASCADE, related_name="report_labels"
    )
    choice = models.ForeignKey[LabelChoice](
        LabelChoice, on_delete=models.PROTECT, related_name="report_labels"
    )
    confidence = models.FloatField(null=True, blank=True)
    rationale = models.TextField(blank=True, default="")
    verified = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["report", "question"],
                name="unique_report_label_per_question",
            )
        ]
        indexes = [
            models.Index(fields=["report", "question"]),
        ]

    def __str__(self) -> str:
        return f"ReportLabel report={self.report_id} question={self.question_id} [{self.pk}]"
