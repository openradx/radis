"""Auto-create default answer options for new questions, and (for now) enqueue
a backfill when a new active question is added.

The auto-backfill-on-create behavior is intentional but will move to a
nightly scheduled job in the trigger-redesign commit. The reasons it does
not make sense to fire on every save in the final design include:

* staff iterate ("add question → run on sample → tweak choices") and an
  immediate backfill bakes a bad prompt into thousands of reports;
* a burst of related question creates should be batched into one LLM call
  per report, not chained sequentially;
* an edit of an existing question should also re-label, but post_save
  cannot distinguish "edit" from "create";
* live ingest of new reports must not be starved by a bursty backfill;
* a backfill is expensive and should be staffed-confirmed (or scheduled
  off-peak) before committing the API budget.

The serialized dedup below (``select_for_update`` on the set row) stays
relevant in the redesign too — it prevents two concurrent question creates
from each spawning a backfill for the same set.
"""

from __future__ import annotations

import logging

from django.conf import settings
from django.db import transaction
from django.db.models.signals import post_save
from django.dispatch import receiver
from django.utils import timezone

from .constants import DEFAULT_ANSWER_OPTIONS
from .models import AnswerOption, BackfillJob, Question, QuestionSet
from .tasks import enqueue_question_set_backfill

logger = logging.getLogger(__name__)


@receiver(post_save, sender=Question)
def update_last_edited_at(sender, instance: Question, **kwargs) -> None:
    """Mark the parent set as recently edited so nightly cron can pick it up."""
    QuestionSet.objects.filter(id=instance.question_set_id).update(
        last_edited_at=timezone.now()
    )


@receiver(post_save, sender=Question)
def enqueue_backfill_for_new_question(
    sender, instance: Question, created: bool, **kwargs
) -> None:
    if not created:
        return
    if not instance.is_active:
        return
    if not settings.LABELS_AUTO_BACKFILL_ON_NEW_QUESTION:
        return

    with transaction.atomic():
        # Lock the set row so two concurrent question-create signals don't
        # both pass the dedup check before either commits.
        QuestionSet.objects.select_for_update().filter(id=instance.question_set_id).first()

        active_exists = BackfillJob.objects.filter(
            question_set_id=instance.question_set_id,
            status__in=[BackfillJob.Status.PENDING, BackfillJob.Status.IN_PROGRESS],
        ).exists()

        if active_exists:
            logger.info(
                "Skipping backfill for set %s — active backfill already exists.",
                instance.question_set_id,
            )
            return

        backfill_job = BackfillJob.objects.create(question_set_id=instance.question_set_id)

        transaction.on_commit(
            lambda: enqueue_question_set_backfill.defer(
                question_set_id=instance.question_set_id,
                backfill_job_id=backfill_job.id,
            )
        )


@receiver(post_save, sender=Question)
def ensure_default_answer_options(
    sender, instance: Question, created: bool, **kwargs
) -> None:
    if not created:
        return
    if instance.options.exists():
        return

    AnswerOption.objects.bulk_create(
        [
            AnswerOption(
                question=instance,
                value=option["value"],
                label=option["label"],
                is_unknown=option["is_unknown"],
                order=option["order"],
            )
            for option in DEFAULT_ANSWER_OPTIONS
        ]
    )
