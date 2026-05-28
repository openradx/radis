from unittest.mock import patch

import pytest
from django.contrib.auth import get_user_model

from radis.pgsearch.models import EmbeddingJob, EmbeddingTask, ReportSearchVector
from radis.pgsearch.tasks import process_embedding_job as _wrapped
from radis.reports.factories import ReportFactory

User = get_user_model()
process_embedding_job = _wrapped.__wrapped__  # type: ignore[attr-defined]
pytestmark = pytest.mark.django_db


def _new_job() -> EmbeddingJob:
    owner = User.objects.get(username="system")
    return EmbeddingJob.objects.create(owner=owner, status=EmbeddingJob.Status.PREPARING)


def _make_pending_reports(n: int):
    reports = [ReportFactory.create() for _ in range(n)]
    # ReportFactory triggers the FTS post_save signal which creates ReportSearchVector
    # rows with embedding=NULL; that's exactly the pending state we want.
    return reports


def test_process_embedding_job_batches_pending_reports(settings):
    settings.EMBEDDING_BATCH_SIZE = 2
    job = _new_job()
    reports = _make_pending_reports(5)

    with patch("radis.pgsearch.models.EmbeddingTask.delay") as delay_mock:
        process_embedding_job(job.id)

    job.refresh_from_db()
    assert job.status == EmbeddingJob.Status.PENDING
    # ceil(5 / 2) = 3 tasks
    assert job.tasks.count() == 3
    # All tasks are dispatched
    assert delay_mock.call_count == 3
    # Every pending report is in exactly one task
    covered = set()
    for task in job.tasks.all():
        covered.update(task.reports.values_list("pk", flat=True))
    assert covered == {r.pk for r in reports}


def test_process_embedding_job_resume_path_only_redispatches_pending_tasks(settings):
    settings.EMBEDDING_BATCH_SIZE = 2
    job = _new_job()
    reports = _make_pending_reports(2)
    # Simulate a previous orchestrator run that created one task already.
    existing = EmbeddingTask.objects.create(job=job, status=EmbeddingTask.Status.PENDING)
    existing.reports.set(reports)
    succeeded = EmbeddingTask.objects.create(job=job, status=EmbeddingTask.Status.SUCCESS)

    with patch("radis.pgsearch.models.EmbeddingTask.delay") as delay_mock:
        process_embedding_job(job.id)

    job.refresh_from_db()
    assert job.status == EmbeddingJob.Status.PENDING
    # No new tasks created
    assert job.tasks.count() == 2
    # Only the pending one is dispatched
    assert delay_mock.call_count == 1


def test_process_embedding_job_with_no_pending_rows():
    job = _new_job()
    # No reports exist → no ReportSearchVector rows with embedding IS NULL.

    with patch("radis.pgsearch.models.EmbeddingTask.delay") as delay_mock:
        process_embedding_job(job.id)

    job.refresh_from_db()
    assert job.status == EmbeddingJob.Status.PENDING
    assert job.tasks.count() == 0
    assert delay_mock.call_count == 0
