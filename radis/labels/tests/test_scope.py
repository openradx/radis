import pytest

from radis.labels.factories import (
    GateAnswerFactory,
    LabelFactory,
    LabelGroupFactory,
    LabelResultFactory,
)
from radis.labels.models import GateAnswer, LabelGroup, LabelResult
from radis.reports.factories import ReportFactory


def _active_group_count():
    return LabelGroup.objects.filter(labels__active=True).distinct().count()


def _ids():
    from radis.labels.scope import _needs_work_queryset

    return list(_needs_work_queryset(_active_group_count()).values_list("pk", flat=True))


@pytest.mark.django_db
def test_report_with_no_gate_answers_needs_work():
    group = LabelGroupFactory.create()
    LabelFactory.create(group=group)
    report = ReportFactory.create()
    assert report.pk in _ids()


@pytest.mark.django_db
def test_report_with_fresh_no_gate_and_no_results_is_done():
    group = LabelGroupFactory.create()
    LabelFactory.create(group=group)
    report = ReportFactory.create()
    GateAnswerFactory.create(report=report, label_group=group, value=GateAnswer.Value.NO)
    assert report.pk not in _ids()


@pytest.mark.django_db
def test_report_with_fresh_yes_gate_but_missing_result_needs_work():
    group = LabelGroupFactory.create()
    LabelFactory.create(group=group)
    report = ReportFactory.create()
    GateAnswerFactory.create(report=report, label_group=group, value=GateAnswer.Value.YES)
    assert report.pk in _ids()


@pytest.mark.django_db
def test_report_with_fresh_yes_gate_and_fresh_results_is_done():
    group = LabelGroupFactory.create()
    label = LabelFactory.create(group=group)
    report = ReportFactory.create()
    GateAnswerFactory.create(report=report, label_group=group, value=GateAnswer.Value.YES)
    LabelResultFactory.create(report=report, label=label, value=LabelResult.Value.PRESENT)
    assert report.pk not in _ids()


@pytest.mark.django_db
def test_report_with_fresh_yes_gate_and_stale_result_needs_work():
    group = LabelGroupFactory.create()
    label = LabelFactory.create(group=group)
    report = ReportFactory.create()
    GateAnswerFactory.create(report=report, label_group=group, value=GateAnswer.Value.YES)
    LabelResultFactory.create(report=report, label=label, value=LabelResult.Value.PRESENT)
    label.description = "edited"
    label.save()
    assert report.pk in _ids()


@pytest.mark.django_db
def test_report_with_only_absent_but_fresh_results_is_done():
    group = LabelGroupFactory.create()
    label = LabelFactory.create(group=group)
    report = ReportFactory.create()
    GateAnswerFactory.create(report=report, label_group=group, value=GateAnswer.Value.YES)
    LabelResultFactory.create(report=report, label=label, value=LabelResult.Value.ABSENT)
    assert report.pk not in _ids()


@pytest.mark.django_db
def test_report_with_stale_gate_needs_work():
    group = LabelGroupFactory.create()
    LabelFactory.create(group=group)
    report = ReportFactory.create()
    GateAnswerFactory.create(report=report, label_group=group, value=GateAnswer.Value.NO)
    group.gate_question = "changed?"
    group.save()
    assert report.pk in _ids()


@pytest.mark.django_db
def test_report_with_one_fresh_gate_and_one_missing_gate_needs_work():
    # Two active groups; gate answered for only one -> non_stale_gate_count (1) < count (2).
    # Anchors the distinct=True rationale: the labels__active join must not inflate the count.
    group_a = LabelGroupFactory.create()
    LabelFactory.create(group=group_a)
    LabelFactory.create(group=group_a)  # 2 active labels -> would inflate count without distinct
    group_b = LabelGroupFactory.create()
    LabelFactory.create(group=group_b)
    report = ReportFactory.create()
    # Fresh NO gate for group_a only (counts as one non-stale gate); group_b has none.
    GateAnswerFactory.create(report=report, label_group=group_a, value=GateAnswer.Value.NO)

    assert _active_group_count() == 2
    assert report.pk in _ids()
