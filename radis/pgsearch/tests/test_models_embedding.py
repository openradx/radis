import pytest
from django.contrib.auth import get_user_model

from radis.pgsearch.models import EmbeddingJob, EmbeddingTask
from radis.reports.factories import ReportFactory

User = get_user_model()
pytestmark = pytest.mark.django_db


def _system_user() -> "User":
    return User.objects.get(username="system")


def test_embedding_job_defaults():
    job = EmbeddingJob.objects.create(owner=_system_user())
    assert job.status == EmbeddingJob.Status.UNVERIFIED
    assert job.urgent is False
    assert job.send_finished_mail is False
    assert job.queued_job_id is None


def test_embedding_task_links_to_reports():
    job = EmbeddingJob.objects.create(owner=_system_user())
    reports = [ReportFactory.create() for _ in range(3)]
    task = EmbeddingTask.objects.create(job=job)
    task.reports.set(reports)
    assert task.status == EmbeddingTask.Status.PENDING
    assert set(task.reports.values_list("pk", flat=True)) == {r.pk for r in reports}
    assert task.attempts == 0
    assert task.queued_job_id is None
