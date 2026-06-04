from django.db import models

from radis.reports.models import Report


class LabelGroup(models.Model):
    id: int
    name = models.CharField(max_length=100, unique=True)
    gate_question = models.TextField()  # upfront Yes/No/Maybe screening question for this group
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


class GateAnswer(models.Model):
    class Value(models.TextChoices):
        YES = "YES", "Yes"
        NO = "NO", "No"
        MAYBE = "MAYBE", "Maybe"

    label_group_id: int
    report = models.ForeignKey(Report, on_delete=models.CASCADE, related_name="gate_answers")
    label_group = models.ForeignKey(
        LabelGroup, on_delete=models.CASCADE, related_name="gate_answers"
    )
    value = models.CharField(max_length=5, choices=Value.choices)
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
