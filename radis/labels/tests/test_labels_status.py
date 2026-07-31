import pytest
from django.core.management import call_command

from radis.labels.factories import LabelFactory, LabelGroupFactory, LabelResultFactory
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
