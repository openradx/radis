from unittest.mock import MagicMock, patch

import numpy as np
import pytest
from django.contrib.auth import get_user_model

from radis.pgsearch.models import EmbeddingJob, EmbeddingTask, ReportSearchVector
from radis.pgsearch.tasks import process_embedding_task as _wrapped
from radis.pgsearch.utils.embedding_client import EmbeddingClientError
from radis.reports.factories import ReportFactory

User = get_user_model()
process_embedding_task = _wrapped.__wrapped__  # type: ignore[attr-defined]
pytestmark = pytest.mark.django_db


def _make_task() -> EmbeddingTask:
    owner = User.objects.get(username="system")
    job = EmbeddingJob.objects.create(owner=owner)
    task = EmbeddingTask.objects.create(job=job)
    reports = [ReportFactory.create() for _ in range(2)]
    task.reports.set(reports)
    return task


def _unit_vec(dim: int) -> list[float]:
    v = np.ones(dim, dtype=np.float32)
    return (v / np.linalg.norm(v)).tolist()


def test_process_embedding_task_writes_vectors_and_marks_success(settings):
    task = _make_task()
    vec = _unit_vec(settings.EMBEDDING_DIM)
    fake_client = MagicMock()
    fake_client.embed_documents.return_value = [vec, vec]
    with patch("radis.pgsearch.tasks.EmbeddingClient", return_value=fake_client):
        process_embedding_task(task.pk)

    task.refresh_from_db()
    assert task.status == EmbeddingTask.Status.SUCCESS
    assert task.queued_job_id is None
    for report in task.reports.all():
        rsv = ReportSearchVector.objects.get(report=report)
        assert rsv.embedding is not None


def test_process_embedding_task_failure_sets_status_and_raises():
    task = _make_task()
    fake_client = MagicMock()
    fake_client.embed_documents.side_effect = EmbeddingClientError("boom")
    with patch("radis.pgsearch.tasks.EmbeddingClient", return_value=fake_client):
        with pytest.raises(EmbeddingClientError):
            process_embedding_task(task.pk)

    task.refresh_from_db()
    assert task.status == EmbeddingTask.Status.FAILURE
    assert task.queued_job_id is None
    assert "boom" in task.message


def test_process_embedding_task_calls_update_job_state(settings):
    task = _make_task()
    vec = _unit_vec(settings.EMBEDDING_DIM)
    fake_client = MagicMock()
    fake_client.embed_documents.return_value = [vec, vec]
    with patch("radis.pgsearch.tasks.EmbeddingClient", return_value=fake_client):
        process_embedding_task(task.pk)

    task.job.refresh_from_db()
    # All tasks succeeded; AnalysisJob.update_job_state rolls up to SUCCESS.
    assert task.job.status == EmbeddingJob.Status.SUCCESS
