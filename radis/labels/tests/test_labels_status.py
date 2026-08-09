import pytest
from django.core.management import call_command

from radis.labels.factories import (
    GateAnswerFactory,
    LabelFactory,
    LabelGroupFactory,
    LabelResultFactory,
)
from radis.labels.models import LabelResult
from radis.reports.factories import ReportFactory


@pytest.mark.django_db
def test_labels_status_reports_counts(capsys) -> None:
    group = LabelGroupFactory.create(name="Chest")
    label = LabelFactory.create(group=group, name="edema")
    report = ReportFactory.create()
    LabelResultFactory.create(report=report, label=label, value=LabelResult.Value.PRESENT)

    call_command("labels_status")
    out = capsys.readouterr().out

    assert "edema" in out
    assert "Present" in out or "PRESENT" in out
    assert "never" in out.lower() or "last scan" in out.lower()


@pytest.mark.django_db
def test_labels_status_counts_report_side_staleness(capsys) -> None:
    group = LabelGroupFactory.create(name="Chest")
    label = LabelFactory.create(group=group, name="edema")
    report = ReportFactory.create()
    LabelResultFactory.create(report=report, label=label, value=LabelResult.Value.PRESENT)
    GateAnswerFactory.create(report=report, label_group=group)

    report.body = "changed"
    report.save()  # result and gate now predate report.updated_at

    call_command("labels_status")
    out = capsys.readouterr().out

    assert "1 stale" in out
    assert "0 stale" not in out
