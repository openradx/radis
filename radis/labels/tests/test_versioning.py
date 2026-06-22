"""Tests for the Question.version provenance contract (HIGH #6 fix).

The system makes one promise to anyone reading old Answer rows:
``Answer.question_version`` tells you which prompt the LLM saw when it
produced this answer. That prompt is built from the question text AND
the AnswerOption rows hanging off the question (see
``radis.labels.schemas.render_questions_for_prompt``). So any edit that
changes either of those must bump ``Question.version``, or the
provenance promise is broken: old answers and new answers carry the
same ``question_version`` despite having been produced under different
prompts.

Pre-HIGH-#6 the version bump only fired for label/question text edits.
These tests pin that AnswerOption mutations bump it too — and that the
initial-default-options creation does NOT bump it (otherwise a brand-new
question would land at version=4 instead of version=1).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from radis.labels.models import (
    Answer,
    AnswerOption,
    LabelingRun,
    Question,
    QuestionSet,
)
from radis.reports.models import Language, Report


def _make_set_with_question() -> tuple[QuestionSet, Question]:
    """Helper that creates a QuestionSet + Question. The Question save
    triggers ``ensure_default_answer_options`` which bulk_creates three
    default options; ``bulk_create`` bypasses post_save so the Question
    stays at version=1 after this helper returns.
    """
    qs = QuestionSet.objects.create(name="VersionedSet")
    q = Question.objects.create(question_set=qs, label="PE present?")
    return qs, q


@pytest.mark.django_db
class TestInitialDefaultOptionsDoNotBumpVersion:
    """The bulk_create in ensure_default_answer_options must NOT trip the
    post_save signal that bumps version. If a future refactor switches
    that path to individual ``save()`` calls, a brand-new question would
    end up at version=4 instead of version=1, which would break the
    "fresh question starts at version=1" invariant.
    """

    def test_brand_new_question_is_version_1(self):
        _, q = _make_set_with_question()
        assert q.version == 1
        # Sanity: the default options were actually created.
        assert q.options.count() == 3

    def test_default_options_creation_does_not_bump_version(self):
        """Same test under a different name to make the intent explicit."""
        qs = QuestionSet.objects.create(name="S")
        q = Question.objects.create(question_set=qs, label="Q1")
        q.refresh_from_db()
        assert q.version == 1


@pytest.mark.django_db
class TestAnswerOptionPostSaveBumpsVersion:
    """Editing an AnswerOption via individual save() — the path the
    Django admin and any future UI use — must bump ``Question.version``.
    """

    def test_editing_existing_option_label_bumps_version(self):
        _, q = _make_set_with_question()
        starting_version = q.version
        option = q.options.first()

        option.label = "Yes — definitely"
        option.save()

        q.refresh_from_db()
        assert q.version == starting_version + 1

    def test_editing_existing_option_value_bumps_version(self):
        """Changing ``value`` is the most dangerous edit: it rewrites the
        LLM enum directly. The version must bump so old answers tagged
        with the prior value stay distinguishable from new ones.
        """
        _, q = _make_set_with_question()
        starting_version = q.version
        option = q.options.first()

        option.value = "definitely_yes"
        option.save()

        q.refresh_from_db()
        assert q.version == starting_version + 1

    def test_editing_is_unknown_bumps_version(self):
        """``is_unknown`` flips which option is the fallback for the
        LLM's "I can't decide" branch. That's a prompt-rendering and
        resolver-behavior change, so it bumps too.
        """
        _, q = _make_set_with_question()
        starting_version = q.version
        option = q.options.filter(is_unknown=False).first()

        option.is_unknown = True
        option.save()

        q.refresh_from_db()
        assert q.version == starting_version + 1

    def test_creating_new_option_via_save_bumps_version(self):
        """A fresh option added through ``save()`` (i.e. NOT via the
        ``bulk_create`` path used by ``ensure_default_answer_options``)
        must bump. This is what happens when an admin adds a fourth
        option through the form.
        """
        _, q = _make_set_with_question()
        starting_version = q.version

        AnswerOption.objects.create(
            question=q, value="maybe", label="Maybe", is_unknown=False, order=4
        )

        q.refresh_from_db()
        assert q.version == starting_version + 1

    def test_multiple_option_edits_accumulate(self):
        """Each save bumps once, sequential edits keep climbing."""
        _, q = _make_set_with_question()
        starting_version = q.version

        for option in q.options.all():
            option.label = option.label + "!"
            option.save()

        q.refresh_from_db()
        assert q.version == starting_version + 3


@pytest.mark.django_db
class TestAnswerOptionPostDeleteBumpsVersion:
    """Deleting an option also changes the LLM enum and must bump."""

    def test_deleting_unused_option_bumps_version(self):
        """``Answer.option`` is PROTECT, so deletes are only possible
        before any Answer rows reference the option. The bump still
        needs to happen so subsequent runs are tagged with the new
        version.
        """
        _, q = _make_set_with_question()
        starting_version = q.version

        # Add a 4th option (we shouldn't delete a default one if we
        # could avoid it; the test stays cleaner by deleting the new one).
        extra = AnswerOption.objects.create(
            question=q, value="maybe", label="Maybe", is_unknown=False, order=4
        )
        q.refresh_from_db()
        version_after_add = q.version
        assert version_after_add == starting_version + 1

        extra.delete()
        q.refresh_from_db()
        assert q.version == version_after_add + 1


@pytest.mark.django_db
class TestLabelsSeedBumpsVersion:
    """``labels_seed`` re-runs are idempotent on identical input but must
    bump when the seed file changes the question text. Otherwise a re-seed
    that updates prompts would silently rewrite the schema and break the
    Answer.question_version contract for prior answers.
    """

    def _write_seed(self, tmp_path: Path, question_text: str) -> Path:
        payload = {
            "question_sets": [
                {
                    "name": "Findings",
                    "questions": [
                        {
                            "label": "Pulmonary embolism",
                            "question": question_text,
                        }
                    ],
                }
            ]
        }
        path = tmp_path / "seed.json"
        path.write_text(json.dumps(payload))
        return path

    def test_identical_reseed_does_not_bump(self, tmp_path: Path):
        from django.core.management import call_command

        seed_path = self._write_seed(tmp_path, "Pulmonary embolism present?")

        call_command("labels_seed", file=str(seed_path))
        q = Question.objects.get(label="Pulmonary embolism")
        first_version = q.version

        call_command("labels_seed", file=str(seed_path))
        q.refresh_from_db()
        assert q.version == first_version

    def test_changing_question_text_in_seed_bumps_version(self, tmp_path: Path):
        from django.core.management import call_command

        first = self._write_seed(tmp_path, "Pulmonary embolism present?")
        call_command("labels_seed", file=str(first))
        q = Question.objects.get(label="Pulmonary embolism")
        first_version = q.version

        second = self._write_seed(tmp_path, "Is pulmonary embolism described?")
        call_command("labels_seed", file=str(second))
        q.refresh_from_db()
        assert q.version == first_version + 1


@pytest.mark.django_db
class TestAnswerSnapshotsCorrectVersion:
    """End-to-end pin: an Answer written after a version bump must carry
    the new version on ``Answer.question_version``. The bump is useless
    if the snapshot doesn't capture it.

    This test creates a synthetic Answer row directly (bypassing the LLM
    processor) to verify the snapshot mechanic; the processor itself
    reads ``question.version`` and writes it onto Answer, which other
    tests already cover in test_dual_mode.
    """

    def _make_report(self) -> Report:
        lang, _ = Language.objects.get_or_create(code="en")
        return Report.objects.create(
            document_id="doc-1",
            body="body",
            patient_birth_date="2000-01-01",
            patient_sex="M",
            study_datetime="2024-01-15T10:00:00Z",
            language=lang,
        )

    def test_answer_carries_current_question_version(self):
        qs, q = _make_set_with_question()
        report = self._make_report()

        # First answer: under version 1.
        run1 = LabelingRun.objects.create(
            report=report,
            question_set=qs,
            mode=LabelingRun.Mode.DIRECT,
            status=LabelingRun.Status.SUCCESS,
        )
        a1 = Answer.objects.create(
            run=run1,
            report=report,
            question=q,
            question_version=q.version,
            option=q.options.first(),
        )

        # Edit an AnswerOption — should bump to version 2.
        option = q.options.first()
        option.label = "Yes — confirmed"
        option.save()
        q.refresh_from_db()

        # Second answer (new run): under version 2.
        run2 = LabelingRun.objects.create(
            report=report,
            question_set=qs,
            mode=LabelingRun.Mode.DIRECT,
            status=LabelingRun.Status.SUCCESS,
        )
        a2 = Answer.objects.create(
            run=run2,
            report=report,
            question=q,
            question_version=q.version,
            option=q.options.first(),
        )

        # The two answers carry distinct version snapshots — the prompt
        # they were produced under is now traceable.
        assert a1.question_version != a2.question_version
        assert a2.question_version == a1.question_version + 1
