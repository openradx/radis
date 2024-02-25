from django.contrib.contenttypes.fields import GenericForeignKey
from django.contrib.contenttypes.models import ContentType
from django.db import models
from django.db.models.constraints import UniqueConstraint


class CoreSettings(models.Model):
    id: int
    maintenance_mode = models.BooleanField(default=False)
    announcement = models.TextField(blank=True)

    class Meta:
        verbose_name_plural = "Core settings"

    def __str__(self):
        return f"{self.__class__.__name__} [ID {self.id}]"

    @classmethod
    def get(cls):
        return cls.objects.first()


class AppSettings(models.Model):
    id: int
    locked = models.BooleanField(default=False)

    class Meta:
        abstract = True

    @classmethod
    def get(cls):
        return cls.objects.first()


class QueuedTask(models.Model):
    id: int
    processor_name = models.CharField(max_length=100)
    content_type = models.ForeignKey(ContentType, on_delete=models.CASCADE)
    object_id = models.PositiveIntegerField()
    content_object = GenericForeignKey("content_type", "object_id")
    priority = models.PositiveIntegerField()
    eta = models.DateTimeField(blank=True, null=True)
    locked = models.BooleanField(default=False)
    kill = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            UniqueConstraint(
                fields=["content_type", "object_id"],
                name="unique_queued_task_per_object",
            )
        ]
        indexes = [
            models.Index(
                fields=["locked", "eta", "priority", "created_at"],
                name="fetch_next_task_idx",
            )
        ]

    def __str__(self) -> str:
        return f"{self.__class__.__name__} [ID {self.id} ({self.content_object})]"
