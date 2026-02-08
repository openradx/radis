from __future__ import annotations

from django.conf import settings
from django.db import transaction
from django.db.models.signals import post_save
from django.dispatch import receiver

from .constants import DEFAULT_LABEL_CHOICES
from .models import LabelChoice, LabelQuestion
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

    transaction.on_commit(
        lambda: enqueue_label_group_backfill.defer(label_group_id=instance.group_id)
    )


@receiver(post_save, sender=LabelQuestion)
def ensure_default_choices(
    sender, instance: LabelQuestion, created: bool, **kwargs
) -> None:
    if not created:
        return
    if instance.choices.exists():
        return

    choices = [
        LabelChoice(
            question=instance,
            value=choice["value"],
            label=choice["label"],
            is_unknown=choice["is_unknown"],
            order=choice["order"],
        )
        for choice in DEFAULT_LABEL_CHOICES
    ]
    LabelChoice.objects.bulk_create(choices)
