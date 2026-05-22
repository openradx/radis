from django.db import models


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
