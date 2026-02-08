from __future__ import annotations

from django.conf import settings
from django.db.models.signals import post_save
from django.dispatch import receiver

from .models import LabelQuestion
from .tasks import enqueue_label_group_backfill


@receiver(post_save, sender=LabelQuestion)
def enqueue_backfill_for_new_question(
    sender, instance: LabelQuestion, created: bool, **kwargs
) -> None:
    if not created:
        return
    if not instance.is_active:
        return
    if not settings.LABELS_AUTO_BACKFILL_ON_NEW_QUESTION:
        return

    enqueue_label_group_backfill.defer(label_group_id=instance.group_id)
