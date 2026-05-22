from django.db import models

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
