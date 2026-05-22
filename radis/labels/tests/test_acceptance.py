import time

import pytest

from radis.labels.factories import QuestionFactory
from radis.labels.models import Answer
from radis.reports.factories import ReportFactory


@pytest.mark.acceptance
def test_ingest_path_labels_a_report_end_to_end():
    from radis.labels.signals import _label_reports_handler

    q = QuestionFactory(
        label="lungs_clear",
        text="Are the lungs clear?",
        group="lung",
        active=True,
    )
    report = ReportFactory(body="No abnormalities, lungs clear.")

    # ReportFactory uses ORM .create() which doesn't fire the reports API handlers.
    # Invoke the labels handler directly to simulate what an ingest API call would trigger.
    _label_reports_handler([report])

    deadline = time.time() + 60
    while time.time() < deadline:
        a = Answer.objects.filter(report=report, question=q).first()
        if a is not None:
            break
        time.sleep(1.0)
    else:
        pytest.fail("No Answer appeared within 60s")
    assert a.value == "YES"
