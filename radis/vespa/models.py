from typing import Callable

from django.db import models


class VespaReFeed(models.Model):
    PENDING = "PE"
    IN_PROGRESS = "IP"
    SUCCESS = "SU"
    FAILURE = "FA"
    STATUS_CHOICES = (
        (PENDING, "Pending"),
        (IN_PROGRESS, "In Progress"),
        (SUCCESS, "Success"),
        (FAILURE, "Failure"),
    )

    id: int
    get_status_display: Callable[[], str]
    status = models.CharField(max_length=2, choices=STATUS_CHOICES, default=PENDING)
    log = models.TextField(blank=True, default="")
    total_count = models.PositiveIntegerField(default=0)
    progress_count = models.PositiveIntegerField(default=0)
    created = models.DateTimeField(auto_now_add=True)
    started = models.DateTimeField(blank=True, null=True)
    ended = models.DateTimeField(blank=True, null=True)

    class Meta:
        indexes = [models.Index(fields=["created"]), models.Index(fields=["status"])]

    def __str__(self) -> str:
        return f"Vespa Re-feed {self.id} [{self.get_status_display()}]"
