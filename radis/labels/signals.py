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

from django.db.models import F
from django.db.models.signals import post_delete, post_save
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

    # NOTE on the version-bump signals below: bulk_create does NOT fire
    # post_save signals by default (Django documents this), so this batch
    # of three default options will not trip the bump-version receiver
    # below. That is the desired behavior — a freshly-created question
    # starts at version=1 (its model default), not version=4. The version
    # bump signal exists for staff edits via the admin or future UI, not
    # for the system-initiated initial seed.
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


@receiver(post_save, sender=AnswerOption)
def bump_question_version_on_answer_option_save(
    sender, instance: AnswerOption, created: bool, **kwargs
) -> None:
    """HIGH #6 fix. Editing an AnswerOption changes the LLM prompt — the
    schema's ``render_questions_for_prompt`` includes the option ``value``
    and ``label`` for every choice — so the prompt the LLM sees is
    materially different than it was before the edit. ``Question.version``
    is the provenance hook that distinguishes "this Answer was produced
    under the prior prompt" from "this Answer was produced under the new
    prompt"; without bumping it here, the two are indistinguishable in
    the database and ``Answer.question_version`` lies about the prompt.

    Triggered by:

    * Adding a new option to an existing question (the admin or a future
      UI). ``created=True`` lands here.
    * Editing the ``value``, ``label``, ``is_unknown``, or ``order`` of
      an existing option. ``created=False`` lands here.

    Not triggered by:

    * The initial three-option creation by
      :func:`ensure_default_answer_options` (uses ``bulk_create``, which
      bypasses ``post_save`` by design). A brand-new question stays at
      version=1.
    """
    instance.question.bump_version()


@receiver(post_delete, sender=AnswerOption)
def bump_question_version_on_answer_option_delete(
    sender, instance: AnswerOption, **kwargs
) -> None:
    """Mirror of the post_save bump: removing an option also changes the
    LLM enum, so the next run's prompt is materially different from the
    prior run's. Existing ``Answer`` rows continue to point at their
    snapshotted version, so they remain attributable to the schema that
    produced them.

    Note: ``Answer.option`` is ``on_delete=PROTECT``, so this signal
    only fires when the option being deleted has no Answer rows
    pointing at it. The schema can still be edited freely *before* any
    labelling has happened; once Answers exist, deletes are blocked and
    staff must deactivate via a different mechanism (e.g. an ``is_active``
    flag, not yet implemented).
    """
    Question.objects.filter(pk=instance.question_id).update(version=F("version") + 1)
