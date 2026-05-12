"""Question lifecycle signals.

Backfill scheduling used to live here as a post_save handler on Question:
adding an active question would immediately create a BackfillJob and defer
the coordinator. That was wrong in production:

* Staff iterate ("add question -> sample -> tweak choices") and an
  immediate backfill bakes a half-finished prompt into thousands of
  reports before anyone reads the first answer.
* A burst of related question creates produces N back-to-back backfills
  per set instead of batching them into one off-peak pass.
* An edit to an existing question is also a semantic change worth
  re-labelling, but post_save cannot distinguish "edit" from "create".
* Live ingest of new reports must not be starved by a bursty backfill
  triggered mid-day by a staff edit.
* A misconfigured question becomes permanent across the corpus before
  it is reviewed.

The new model: edits bump ``QuestionSet.last_edited_at``; a nightly
periodic task scans for sets with outstanding work and dispatches one
backfill per dirty set off-peak. See ``tasks.labels_backfill_launcher``.
"""

from __future__ import annotations

from django.db.models.signals import post_save
from django.dispatch import receiver
from django.utils import timezone

from .constants import DEFAULT_ANSWER_OPTIONS
from .models import AnswerOption, Question, QuestionSet


@receiver(post_save, sender=Question)
def update_last_edited_at(sender, instance: Question, **kwargs) -> None:
    """Mark the parent set as recently edited so the nightly launcher can
    detect it as dirty.
    """
    QuestionSet.objects.filter(id=instance.question_set_id).update(
        last_edited_at=timezone.now()
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
