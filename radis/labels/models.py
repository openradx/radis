from __future__ import annotations

from typing import Callable

from django.db import models

from radis.reports.models import Report


class LabelGroup(models.Model):
    name = models.CharField(max_length=100)
    slug = models.SlugField(unique=True)
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
    group = models.ForeignKey[LabelGroup](
        LabelGroup, on_delete=models.CASCADE, related_name="questions"
    )
    name = models.CharField(max_length=100)
    question = models.CharField(max_length=300)
    description = models.CharField(max_length=300, blank=True, default="")
    is_active = models.BooleanField(default=True)
    order = models.PositiveSmallIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    choices: models.QuerySet["LabelChoice"]
    get_name_display: Callable[[], str]

    class Meta:
        ordering = ["order", "name"]
        constraints = [
            models.UniqueConstraint(
                fields=["group", "name"],
                name="unique_label_question_name_per_group",
            )
        ]

    def __str__(self) -> str:
        return f"LabelQuestion {self.name} [{self.pk}]"


class LabelChoice(models.Model):
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
