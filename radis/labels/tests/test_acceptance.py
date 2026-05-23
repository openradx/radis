import pytest
from adit_radis_shared.common.utils.testing_helpers import run_worker_once

from radis.labels.factories import QuestionFactory
from radis.labels.models import Answer
from radis.labels.signals import _label_reports_handler
from radis.reports.factories import ReportFactory


@pytest.mark.acceptance
@pytest.mark.django_db(transaction=True)
def test_ingest_path_labels_a_report_end_to_end():
    """End-to-end smoke test: handler defers a job, an in-process worker drains
    the llm queue, the real ChatClient calls the LLM, and an Answer row is written.

    Uses transaction=True so the deferred procrastinate_job row is committed and
    visible to the worker connector. The worker runs in this test process via
    ``run_worker_once`` (the standard ADIT/RADIS-shared pattern), so it sees
    ``test_postgres`` and doesn't depend on the persistent ``llm_worker`` container.
    """
    q = QuestionFactory(
        label="lungs_clear",
        text="Are the lungs clear?",
        group="lung",
        active=True,
    )
    report = ReportFactory(body="No abnormalities, lungs clear.")

    # ReportFactory uses ORM .create() which doesn't fire the reports API handlers.
    # Invoke the labels handler directly to simulate what an ingest API call would
    # trigger: it defers a Procrastinate job on the 'llm' queue.
    _label_reports_handler([report])

    # Synchronously drain queued jobs in-process.
    run_worker_once()

    answer = Answer.objects.get(report=report, question=q)
    assert answer.value == "YES"
