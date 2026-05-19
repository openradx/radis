"""Tests for the dual-mode labelling pipeline (DIRECT + REASONED).

The processor is exercised against a mocked ChatClient so these tests run
without touching the LLM. The point is to verify that:
  * DIRECT mode issues exactly one structured-output call,
  * REASONED mode issues a free-form call followed by a structured one and
    persists the reasoning text on the run,
  * enqueue_labeling_for_reports dispatches one task per mode.
"""

from unittest.mock import MagicMock, patch

import pytest

from radis.labels.models import (
    Answer,
    AnswerOption,
    LabelingRun,
    Question,
    QuestionSet,
)
from radis.labels.processors import LabelingProcessor
from radis.reports.models import Language, Report


def _make_report(document_id: str = "doc-1") -> Report:
    lang, _ = Language.objects.get_or_create(code="en")
    return Report.objects.create(
        document_id=document_id,
        body="Report findings: pulmonary embolism present.",
        patient_birth_date="2000-01-01",
        patient_sex="M",
        study_datetime="2024-01-15T10:00:00Z",
        language=lang,
    )


def _make_set_with_one_question(name: str = "Findings") -> tuple[QuestionSet, Question]:
    question_set = QuestionSet.objects.create(name=name)
    # Signal auto-creates the 3 default options on Question.save.
    question = Question.objects.create(
        question_set=question_set, label="PE present?"
    )
    return question_set, question


def _fake_response_for_one_question(choice: str = "yes"):
    """Stand-in for the parsed Pydantic response from ``client.extract_data``.

    We don't import the dynamic response model; we just expose
    ``question_0`` with the attributes the processor reads, and a
    ``model_dump`` that returns a JSON-friendly dict.
    """
    answer = MagicMock()
    answer.choice = choice
    answer.confidence = 0.92
    answer.rationale = "Report explicitly mentions pulmonary embolism."

    result = MagicMock()
    result.question_0 = answer
    result.model_dump.return_value = {
        "question_0": {
            "choice": choice,
            "confidence": 0.92,
            "rationale": "Report explicitly mentions pulmonary embolism.",
        }
    }
    return result


@pytest.mark.django_db(transaction=True)
class TestDirectMode:
    def test_direct_mode_makes_one_structured_call_only(self):
        question_set, _ = _make_set_with_one_question()
        report = _make_report()

        processor = LabelingProcessor(question_set, mode=LabelingRun.Mode.DIRECT)
        processor.client.extract_data = MagicMock(  # type: ignore[method-assign]
            return_value=_fake_response_for_one_question("yes")
        )
        processor.client.complete_text = MagicMock()  # type: ignore[method-assign]

        processor.process_reports([report.id])

        # DIRECT must not call the reasoning step.
        assert processor.client.complete_text.call_count == 0
        assert processor.client.extract_data.call_count == 1

        run = LabelingRun.objects.get(
            report=report, question_set=question_set, mode=LabelingRun.Mode.DIRECT
        )
        assert run.status == LabelingRun.Status.SUCCESS
        assert run.reasoning_text == ""
        assert Answer.objects.filter(run=run).count() == 1


@pytest.mark.django_db(transaction=True)
class TestReasonedMode:
    def test_reasoned_mode_makes_reasoning_then_structured_call(self):
        question_set, _ = _make_set_with_one_question()
        report = _make_report()

        processor = LabelingProcessor(question_set, mode=LabelingRun.Mode.REASONED)
        processor.client.complete_text = MagicMock(  # type: ignore[method-assign]
            return_value="Step 1: the report explicitly mentions PE."
        )
        processor.client.extract_data = MagicMock(  # type: ignore[method-assign]
            return_value=_fake_response_for_one_question("yes")
        )

        processor.process_reports([report.id])

        # Reasoning + structured = two calls, in that order.
        assert processor.client.complete_text.call_count == 1
        assert processor.client.extract_data.call_count == 1

        run = LabelingRun.objects.get(
            report=report, question_set=question_set, mode=LabelingRun.Mode.REASONED
        )
        assert run.status == LabelingRun.Status.SUCCESS
        assert "Step 1" in run.reasoning_text

    def test_reasoning_text_is_passed_into_structured_prompt(self):
        """The structured-call prompt must include the reasoning text so the
        second model call can use the chain-of-thought from the first call.
        """
        question_set, _ = _make_set_with_one_question()
        report = _make_report()

        processor = LabelingProcessor(question_set, mode=LabelingRun.Mode.REASONED)
        processor.client.complete_text = MagicMock(  # type: ignore[method-assign]
            return_value="DISTINCTIVE_REASONING_MARKER"
        )
        processor.client.extract_data = MagicMock(  # type: ignore[method-assign]
            return_value=_fake_response_for_one_question("no")
        )

        processor.process_reports([report.id])

        # The second call's first positional arg is the assembled structured prompt.
        structured_prompt = processor.client.extract_data.call_args.args[0]
        assert "DISTINCTIVE_REASONING_MARKER" in structured_prompt


@pytest.mark.django_db(transaction=True)
class TestRunFailureHandling:
    def test_failed_llm_call_marks_run_failure(self):
        question_set, _ = _make_set_with_one_question()
        report = _make_report()

        processor = LabelingProcessor(question_set, mode=LabelingRun.Mode.DIRECT)
        processor.client.extract_data = MagicMock(  # type: ignore[method-assign]
            side_effect=RuntimeError("LLM exploded")
        )

        # process_reports catches and logs; we expect a FAILURE row, no answers.
        processor.process_reports([report.id])

        run = LabelingRun.objects.get(report=report, question_set=question_set)
        assert run.status == LabelingRun.Status.FAILURE
        assert "LLM exploded" in run.error_message
        assert Answer.objects.filter(run=run).count() == 0


@pytest.mark.django_db
class TestEnqueueDispatchesPerMode:
    def test_enqueue_labeling_for_reports_dispatches_one_task_per_mode(self, settings):
        settings.LABELS_RUN_MODES = [LabelingRun.Mode.DIRECT, LabelingRun.Mode.REASONED]
        question_set, _ = _make_set_with_one_question()
        report = _make_report()

        with patch("radis.labels.tasks._defer_batch") as mock_defer:
            from radis.labels.tasks import enqueue_labeling_for_reports

            enqueue_labeling_for_reports([report.id])

        # One task per (set, batch, mode). One set, one batch, two modes => 2 calls.
        assert mock_defer.call_count == 2
        modes_dispatched = {
            call.kwargs["mode"] for call in mock_defer.call_args_list
        }
        assert modes_dispatched == {LabelingRun.Mode.DIRECT, LabelingRun.Mode.REASONED}

    def test_live_enqueue_uses_live_priority(self, settings):
        """Live ingest must run high-priority so dashboards don't wait for a
        backfill to drain. The constant lives in settings.
        """
        settings.LABELS_RUN_MODES = [LabelingRun.Mode.DIRECT]
        settings.LABELS_LIVE_PRIORITY = 99
        question_set, _ = _make_set_with_one_question()
        report = _make_report()

        with patch("radis.labels.tasks._defer_batch") as mock_defer:
            from radis.labels.tasks import enqueue_labeling_for_reports

            enqueue_labeling_for_reports([report.id])

        assert mock_defer.call_count == 1
        assert mock_defer.call_args.kwargs["priority"] == 99


# -- MEDIUM #1 regression: invalid-question filter --


@pytest.mark.django_db(transaction=True)
class TestInvalidQuestionsFilteredBeforeDispatch:
    """A Question with zero AnswerOptions cannot be encoded into the
    LLM's response schema (``Literal[()]`` is not a thing). Pre-MEDIUM-#1
    the schema build would raise inside ``_run_llm_and_persist``, the
    transaction.atomic block would roll back, and the entire report's
    labelling would fail — one broken question killed the whole batch.

    The fix is to filter active-but-empty-option questions out at
    ``process_reports`` entry. These tests pin both branches: a single
    bad question alongside good ones, and an all-bad set.
    """

    def _add_question_without_options(
        self, question_set: QuestionSet, label: str
    ) -> Question:
        """Create a Question whose default options are then deleted, so
        the question is left active with zero options. The default-options
        signal fires on save, so we delete after the fact.
        """
        question = Question.objects.create(question_set=question_set, label=label)
        question.options.all().delete()
        return question

    def test_empty_options_question_is_skipped_other_questions_succeed(self):
        """Two active questions: one valid, one with no options. The
        processor must label only the valid one and write the Answer.
        The broken question stays unanswered (and the report stays
        ``missing_reports``-incomplete) — that's the visible signal to
        ops that a config fix is needed.
        """
        question_set = QuestionSet.objects.create(name="MixedSet")
        good = Question.objects.create(question_set=question_set, label="GoodQ")
        bad = self._add_question_without_options(question_set, "BadQ")
        report = _make_report()

        processor = LabelingProcessor(question_set, mode=LabelingRun.Mode.DIRECT)
        processor.client.extract_data = MagicMock(  # type: ignore[method-assign]
            return_value=_fake_response_for_one_question("yes")
        )
        processor.client.complete_text = MagicMock()  # type: ignore[method-assign]

        processor.process_reports([report.id])

        # The valid question got an Answer; the broken one did not.
        assert Answer.objects.filter(question=good).count() == 1
        assert Answer.objects.filter(question=bad).count() == 0

        # The run still succeeded — the broken question didn't crash the batch.
        run = LabelingRun.objects.get(report=report, question_set=question_set)
        assert run.status == LabelingRun.Status.SUCCESS

    def test_all_questions_empty_options_skips_entire_batch(self):
        """If every active question is invalid there is nothing to ask
        the LLM about. The processor logs and returns without creating
        any LabelingRun row. The report stays ``missing_reports``-incomplete
        until the configuration is fixed; the next nightly tick will
        try again (and skip again, until ops adds options).
        """
        question_set = QuestionSet.objects.create(name="AllBroken")
        self._add_question_without_options(question_set, "BadA")
        self._add_question_without_options(question_set, "BadB")
        report = _make_report()

        processor = LabelingProcessor(question_set, mode=LabelingRun.Mode.DIRECT)
        processor.client.extract_data = MagicMock()  # type: ignore[method-assign]
        processor.client.complete_text = MagicMock()  # type: ignore[method-assign]

        processor.process_reports([report.id])

        # No LLM call should have been made for either method.
        assert processor.client.extract_data.call_count == 0
        assert processor.client.complete_text.call_count == 0
        # No LabelingRun row should have been created — there was nothing
        # to attempt, so no record to attribute success or failure to.
        assert LabelingRun.objects.filter(report=report).count() == 0
        assert Answer.objects.filter(report=report).count() == 0

    def test_empty_options_question_does_not_crash_other_modes(self):
        """REASONED mode does two LLM calls per report (reasoning then
        structured). The invalid-question filter must apply to both —
        if it leaked into the structured-call schema, the entire run
        would fail. Mirror of the DIRECT test for REASONED.
        """
        question_set = QuestionSet.objects.create(name="MixedReasoned")
        good = Question.objects.create(question_set=question_set, label="GoodQ")
        self._add_question_without_options(question_set, "BadQ")
        report = _make_report()

        processor = LabelingProcessor(question_set, mode=LabelingRun.Mode.REASONED)
        processor.client.complete_text = MagicMock(  # type: ignore[method-assign]
            return_value="reasoning text"
        )
        processor.client.extract_data = MagicMock(  # type: ignore[method-assign]
            return_value=_fake_response_for_one_question("yes")
        )

        processor.process_reports([report.id])

        assert Answer.objects.filter(question=good).count() == 1
        run = LabelingRun.objects.get(report=report, question_set=question_set)
        assert run.status == LabelingRun.Status.SUCCESS


# -- MEDIUM #2 regression: NaN-safe confidence --


class TestNormalizeConfidenceNaNSafe:
    """``_normalize_confidence`` must return ``None`` on NaN.

    Pre-MEDIUM-#2 the bounds checks let NaN pass through unchanged
    (because NaN < 0 and NaN > 1 are both False), and the NaN was
    written to ``Answer.confidence``. Downstream ``eval_metrics``
    averaged the column and produced "nan" cells in the Markdown
    report. The fix is one ``math.isnan`` check; these tests pin it.
    """

    def test_nan_returns_none(self):
        from radis.labels.processors import _normalize_confidence

        assert _normalize_confidence(float("nan")) is None

    def test_negative_nan_returns_none(self):
        """NaN has no sign in any useful sense, but the helper must
        still return None whether the input is +nan or -nan.
        """
        from radis.labels.processors import _normalize_confidence

        assert _normalize_confidence(float("-nan")) is None

    def test_none_returns_none_back_compat(self):
        """Regression guard: the None branch must still work after the
        NaN check is added.
        """
        from radis.labels.processors import _normalize_confidence

        assert _normalize_confidence(None) is None

    def test_negative_clamps_to_zero(self):
        from radis.labels.processors import _normalize_confidence

        assert _normalize_confidence(-0.5) == 0.0

    def test_over_one_clamps_to_one(self):
        from radis.labels.processors import _normalize_confidence

        assert _normalize_confidence(1.5) == 1.0

    def test_valid_value_passes_through(self):
        from radis.labels.processors import _normalize_confidence

        assert _normalize_confidence(0.73) == 0.73

    def test_inf_clamps_to_one(self):
        """Infinity is technically not "NaN" but is also not a useful
        confidence value. The existing >1 branch handles it.
        """
        from radis.labels.processors import _normalize_confidence

        assert _normalize_confidence(float("inf")) == 1.0

    def test_negative_inf_clamps_to_zero(self):
        from radis.labels.processors import _normalize_confidence

        assert _normalize_confidence(float("-inf")) == 0.0


@pytest.mark.django_db(transaction=True)
class TestNaNConfidenceDoesNotPropagateToAnswerOrEval:
    """End-to-end regression: an LLM that returns NaN confidence (some
    quantized models do this) must not produce a NaN in
    ``Answer.confidence`` or in the eval-report Markdown. The
    ``_normalize_confidence`` fix runs at write time so the NaN is
    sanitized before persistence.
    """

    def test_nan_from_llm_lands_as_none_in_answer(self):
        question_set, _ = _make_set_with_one_question()
        report = _make_report()

        processor = LabelingProcessor(question_set, mode=LabelingRun.Mode.DIRECT)

        # Fake response whose confidence is NaN.
        nan_response = _fake_response_for_one_question("yes")
        nan_response.question_0.confidence = float("nan")
        processor.client.extract_data = MagicMock(  # type: ignore[method-assign]
            return_value=nan_response
        )
        processor.client.complete_text = MagicMock()  # type: ignore[method-assign]

        processor.process_reports([report.id])

        answer = Answer.objects.get(report=report)
        # The confidence column is nullable; None is the right "we don't
        # have a useful confidence" sentinel.
        assert answer.confidence is None


@pytest.mark.django_db
class TestBothModesCoexistForSameReport:
    def test_two_runs_one_report(self):
        """A report can have one DIRECT and one REASONED run, each with its
        own answers. ``missing_reports`` must require BOTH modes to consider
        the report complete.
        """
        question_set, question = _make_set_with_one_question()
        report = _make_report()
        # First mode: DIRECT only.
        run_di = LabelingRun.objects.create(
            report=report,
            question_set=question_set,
            mode=LabelingRun.Mode.DIRECT,
            status=LabelingRun.Status.SUCCESS,
        )
        option: AnswerOption = question.options.first()  # type: ignore[assignment]
        Answer.objects.create(
            run=run_di,
            report=report,
            question=question,
            question_version=question.version,
            option=option,
        )

        # With DI alone, missing_reports must still return the report because
        # REASONED is also required (per default LABELS_RUN_MODES).
        assert list(question_set.missing_reports()) == [report]

        # Add the REASONED run; now the report is complete.
        run_re = LabelingRun.objects.create(
            report=report,
            question_set=question_set,
            mode=LabelingRun.Mode.REASONED,
            status=LabelingRun.Status.SUCCESS,
        )
        Answer.objects.create(
            run=run_re,
            report=report,
            question=question,
            question_version=question.version,
            option=option,
        )
        assert list(question_set.missing_reports()) == []
