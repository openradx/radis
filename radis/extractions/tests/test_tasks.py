import pytest
from adit_radis_shared.accounts.factories import GroupFactory, UserFactory

from radis.extractions import site as extraction_site
from radis.extractions.models import ExtractionInstance, ExtractionJob, ExtractionTask
from radis.extractions.site import ExtractionRetrievalProvider
from radis.extractions.tasks import process_extraction_job
from radis.pgsearch.providers import count as pgsearch_count
from radis.pgsearch.providers import retrieve as pgsearch_retrieve
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

    monkeypatch.setattr(ExtractionTask, "delay", fake_delay, raising=True)

    process_extraction_job(int(job.pk))

    assert enqueue_job_statuses  # at least one task was enqueued
    assert all(status == ExtractionJob.Status.PENDING for status in enqueue_job_statuses)


@pytest.mark.django_db
def test_process_extraction_job_with_no_language_matches_each_documents_own_config(monkeypatch):
    """An ExtractionJob with ``language=None`` ("All") must not silently
    restrict retrieval to a single language's text-search config -- mirrors
    ``radis.pgsearch.tests.test_language_config
    .test_documents_in_different_languages_are_each_matched_under_their_own_config``,
    but exercised end-to-end through the extraction task pipeline (real
    pgsearch provider, real ``ExtractionTask``/``ExtractionInstance``
    creation) with a job whose language is None.
    """
    user = UserFactory.create(is_active=True)
    group = GroupFactory.create()

    english = ReportFactory.create(
        document_id="EXTRACT-EN",
        body="Findings: large pleural effusion.",
        language=LanguageFactory.create(code="en"),
    )
    english.groups.add(group)
    german = ReportFactory.create(
        document_id="EXTRACT-DE",
        body="Befund: Pleuraergüsse beidseits.",
        language=LanguageFactory.create(code="de"),
    )
    german.groups.add(group)

    provider = ExtractionRetrievalProvider(
        name="pgsearch",
        count=pgsearch_count,
        retrieve=pgsearch_retrieve,
        max_results=100,
    )
    monkeypatch.setattr(extraction_site, "extraction_retrieval_provider", provider)
    monkeypatch.setattr(ExtractionTask, "delay", lambda self: None, raising=True)

    def matched_document_ids(query: str) -> set[str]:
        job = ExtractionJob.objects.create(
            owner=user,
            group=group,
            title="No-language extraction",
            query=query,
            language=None,
            status=ExtractionJob.Status.PENDING,
        )
        process_extraction_job(int(job.pk))
        return set(
            ExtractionInstance.objects.filter(task__job=job).values_list(
                "report__document_id", flat=True
            )
        )

    # Each query term is only stemmed/matched correctly under its own
    # document's language config -- 'effusion' under english, 'Pleuraerguss'
    # under german. With language=None both branches are searched, so each
    # query still reaches the document written in that language.
    assert matched_document_ids("effusion") == {english.document_id}
    assert matched_document_ids("Pleuraerguss") == {german.document_id}
