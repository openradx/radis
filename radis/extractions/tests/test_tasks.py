import pytest
from adit_radis_shared.accounts.factories import GroupFactory, UserFactory

from radis.extractions import site as extraction_site
from radis.extractions.models import ExtractionJob, ExtractionTask
from radis.extractions.site import ExtractionRetrievalProvider
from radis.extractions.tasks import process_extraction_job
from radis.reports.factories import LanguageFactory, ReportFactory


@pytest.mark.django_db
def test_process_extraction_job_only_enqueues_tasks_after_job_is_pending(monkeypatch):
    """
    Regression test for #197:
    Tasks must never be enqueued while the job is still PREPARING.
    """

    user = UserFactory.create(is_active=True)
    group = GroupFactory.create()
    language = LanguageFactory.create(code="en")

    job = ExtractionJob.objects.create(
        owner=user,
        group=group,
        title="Test Extraction",
        query="test",
        language=language,
        status=ExtractionJob.Status.PENDING,
    )

    doc_ids = ["DOC-1", "DOC-2"]
    for doc_id in doc_ids:
        ReportFactory.create(document_id=doc_id)

    provider = ExtractionRetrievalProvider(
        name="dummy",
        count=lambda _search: len(doc_ids),
        retrieve=lambda _search: doc_ids,
        max_results=100,
    )
    monkeypatch.setattr(extraction_site, "extraction_retrieval_provider", provider)

    enqueue_job_statuses: list[str] = []

    def fake_delay(self: ExtractionTask) -> None:
        enqueue_job_statuses.append(self.job.status)
        self.queued_job_id = 123
        self.save()

    monkeypatch.setattr(ExtractionTask, "delay", fake_delay, raising=True)

    process_extraction_job(int(job.pk))

    assert enqueue_job_statuses  # at least one task was enqueued
    assert all(status == ExtractionJob.Status.PENDING for status in enqueue_job_statuses)
