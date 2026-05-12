"""Tests for the evaluation framework: stratified sampler, metric
computation, and the seed/report management commands.
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest
from adit_radis_shared.accounts.factories import UserFactory
from django.core.management import call_command
from django.test import Client

from radis.labels.models import (
    Answer,
    EvalSample,
    LabelingRun,
    Question,
    QuestionSet,
)
from radis.labels.utils.eval_metrics import compute_eval, render_markdown
from radis.labels.utils.eval_sampler import estimate_calls, sample_reports
from radis.reports.models import Language, Report


def _make_report(document_id: str, year: int = 2024) -> Report:
    lang, _ = Language.objects.get_or_create(code="en")
    return Report.objects.create(
        document_id=document_id,
        body="Test body",
        patient_birth_date="2000-01-01",
        patient_sex="M",
        study_datetime=f"{year}-06-15T10:00:00Z",
        language=lang,
    )


def _make_question(question_set: QuestionSet, label: str) -> Question:
    return Question.objects.create(question_set=question_set, label=label)


def _record_answer(
    report: Report,
    question_set: QuestionSet,
    question: Question,
    mode: str,
    choice_value: str = "yes",
    confidence: float = 0.9,
    reasoning_text: str = "",
) -> Answer:
    """Materialize one SUCCESS run + one answer for the given mode."""
    run = LabelingRun.objects.create(
        report=report,
        question_set=question_set,
        mode=mode,
        status=LabelingRun.Status.SUCCESS,
        reasoning_text=reasoning_text,
    )
    option = question.options.get(value=choice_value)
    return Answer.objects.create(
        run=run,
        report=report,
        question=question,
        question_version=question.version,
        option=option,
        confidence=confidence,
    )


# -- Sampler --


@pytest.mark.django_db
class TestSampler:
    def test_sampler_returns_empty_when_no_reports(self):
        assert sample_reports(target_size=10, seed=42) == []

    def test_sampler_returns_all_when_target_exceeds_corpus(self):
        for i in range(5):
            _make_report(f"doc-{i}")
        result = sample_reports(target_size=100, seed=42)
        assert len(result) == 5
        assert set(result) == set(Report.objects.values_list("id", flat=True))

    def test_sampler_is_deterministic_given_seed(self):
        for year in (2020, 2021, 2022, 2023, 2024):
            for i in range(20):
                _make_report(f"doc-{year}-{i}", year=year)
        a = sample_reports(target_size=30, seed=99)
        b = sample_reports(target_size=30, seed=99)
        assert a == b

    def test_sampler_spreads_across_years(self):
        for year in (2020, 2021, 2022, 2023, 2024):
            for i in range(20):
                _make_report(f"doc-{year}-{i}", year=year)
        chosen = sample_reports(target_size=25, seed=42)
        # We don't strictly assert balance, but every year should be present
        # with at least one report given a floor of 5 and 5 years => 25 floor.
        years_seen = {
            Report.objects.get(id=rid).study_datetime.year for rid in chosen
        }
        assert years_seen == {2020, 2021, 2022, 2023, 2024}


def test_estimate_calls_counts_reasoning_as_two():
    estimate = estimate_calls(
        sample_size=100, active_questions=5, modes=["DI", "RE"]
    )
    # DI=1 call, RE=2 calls per report => 3 calls × 100 reports = 300
    assert estimate["total_calls"] == 300


# -- Metrics --


@pytest.mark.django_db
class TestComputeEval:
    def _setup_with_one_question(self):
        question_set = QuestionSet.objects.create(name="Findings")
        question = _make_question(question_set, "PE?")
        report_yes = _make_report("doc-yes")
        report_no = _make_report("doc-no")
        sample = EvalSample.objects.create(
            name="t1", question_set=question_set, target_size=2
        )
        sample.reports.add(report_yes, report_no)
        return question_set, question, report_yes, report_no, sample

    def test_agreement_when_modes_agree(self):
        qs, q, r_yes, r_no, sample = self._setup_with_one_question()
        _record_answer(r_yes, qs, q, LabelingRun.Mode.DIRECT, "yes")
        _record_answer(r_yes, qs, q, LabelingRun.Mode.REASONED, "yes")
        _record_answer(r_no, qs, q, LabelingRun.Mode.DIRECT, "no")
        _record_answer(r_no, qs, q, LabelingRun.Mode.REASONED, "no")

        report = compute_eval(sample)

        assert report["overall"]["n_compared"] == 2
        assert report["overall"]["n_agree"] == 2
        assert report["overall"]["agreement_rate"] == 1.0
        assert report["disagreements"] == []

    def test_disagreement_is_captured(self):
        qs, q, r_yes, r_no, sample = self._setup_with_one_question()
        _record_answer(r_yes, qs, q, LabelingRun.Mode.DIRECT, "yes", confidence=0.99)
        _record_answer(
            r_yes,
            qs,
            q,
            LabelingRun.Mode.REASONED,
            "no",
            confidence=0.85,
            reasoning_text="Actually re-reading the report I disagree.",
        )

        report = compute_eval(sample)

        assert report["overall"]["n_compared"] == 1
        assert report["overall"]["n_agree"] == 0
        assert len(report["disagreements"]) == 1
        first = report["disagreements"][0]
        assert first["direct_choice"] == "Yes"
        assert first["reasoned_choice"] == "No"
        assert "Actually re-reading" in first["reasoning_text"]

    def test_reports_missing_one_mode_are_not_compared(self):
        qs, q, r_yes, r_no, sample = self._setup_with_one_question()
        _record_answer(r_yes, qs, q, LabelingRun.Mode.DIRECT, "yes")
        # No REASONED run for r_yes — should be omitted from comparison.
        _record_answer(r_no, qs, q, LabelingRun.Mode.DIRECT, "no")
        _record_answer(r_no, qs, q, LabelingRun.Mode.REASONED, "no")

        report = compute_eval(sample)
        assert report["overall"]["n_compared"] == 1
        assert report["overall"]["n_agree"] == 1


@pytest.mark.django_db
class TestRenderMarkdown:
    def test_render_includes_set_name_and_per_question_table(self):
        question_set = QuestionSet.objects.create(name="Findings")
        question = _make_question(question_set, "PE?")
        report = _make_report("doc-yes")
        sample = EvalSample.objects.create(
            name="render-1", question_set=question_set, target_size=1
        )
        sample.reports.add(report)
        _record_answer(report, question_set, question, LabelingRun.Mode.DIRECT, "yes")
        _record_answer(report, question_set, question, LabelingRun.Mode.REASONED, "yes")

        rendered = render_markdown(compute_eval(sample))
        assert "Findings" in rendered
        assert "PE?" in rendered
        assert "Overall agreement" in rendered


# -- Management commands --


@pytest.mark.django_db
class TestSeedCommand:
    def test_seed_creates_sample_and_enqueues_for_missing(self):
        question_set = QuestionSet.objects.create(name="Findings")
        _make_question(question_set, "PE?")
        for i in range(3):
            _make_report(f"doc-{i}")

        target = "radis.labels.management.commands.labels_eval_seed.enqueue_labeling_for_reports"
        with patch(target) as mock_enqueue:
            call_command(
                "labels_eval_seed",
                question_set_id=question_set.id,
                sample_size=3,
                name="test-sample",
            )

        sample = EvalSample.objects.get(name="test-sample")
        assert sample.actual_size == 3
        assert mock_enqueue.called
        enqueued_ids = mock_enqueue.call_args.args[0]
        assert set(enqueued_ids) == set(sample.reports.values_list("id", flat=True))

    def test_seed_skips_enqueue_when_all_reports_complete(self):
        question_set = QuestionSet.objects.create(name="Findings")
        question = _make_question(question_set, "PE?")
        report = _make_report("doc-1")
        # Mark this report as fully labelled across both default modes.
        _record_answer(report, question_set, question, LabelingRun.Mode.DIRECT, "yes")
        _record_answer(report, question_set, question, LabelingRun.Mode.REASONED, "yes")

        target = "radis.labels.management.commands.labels_eval_seed.enqueue_labeling_for_reports"
        with patch(target) as mock_enqueue:
            call_command(
                "labels_eval_seed",
                question_set_id=question_set.id,
                sample_size=10,
                name="already-complete",
            )

        assert not mock_enqueue.called


@pytest.mark.django_db
class TestReportCommand:
    def test_report_writes_markdown(self):
        question_set = QuestionSet.objects.create(name="Findings")
        question = _make_question(question_set, "PE?")
        report = _make_report("doc-1")
        sample = EvalSample.objects.create(
            name="report-md-test", question_set=question_set, target_size=1
        )
        sample.reports.add(report)
        _record_answer(report, question_set, question, LabelingRun.Mode.DIRECT, "yes")
        _record_answer(report, question_set, question, LabelingRun.Mode.REASONED, "yes")

        with tempfile.TemporaryDirectory() as tmp:
            call_command(
                "labels_eval_report",
                sample_name="report-md-test",
                output_dir=tmp,
            )
            files = list(Path(tmp).glob("*.md"))
            assert len(files) == 1
            content = files[0].read_text()
            assert "Findings" in content
            assert "PE?" in content


@pytest.mark.django_db
class TestEvalViews:
    def test_question_set_eval_view_no_sample(self, client: Client):
        user = UserFactory.create(is_active=True)
        client.force_login(user)
        question_set = QuestionSet.objects.create(name="Findings")
        response = client.get(f"/labels/{question_set.pk}/eval/")
        assert response.status_code == 200
        assert response.context["sample"] is None

    def test_question_set_eval_view_renders_sample(self, client: Client):
        user = UserFactory.create(is_active=True)
        client.force_login(user)
        question_set = QuestionSet.objects.create(name="Findings")
        question = _make_question(question_set, "PE?")
        report = _make_report("doc-1")
        sample = EvalSample.objects.create(
            name="view-test", question_set=question_set, target_size=1
        )
        sample.reports.add(report)
        _record_answer(report, question_set, question, LabelingRun.Mode.DIRECT, "yes")
        _record_answer(report, question_set, question, LabelingRun.Mode.REASONED, "no")

        response = client.get(f"/labels/{question_set.pk}/eval/")
        assert response.status_code == 200
        assert response.context["sample"] == sample
        assert response.context["report"]["overall"]["n_compared"] == 1
