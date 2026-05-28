from unittest.mock import patch

import pytest
from django.contrib.auth import get_user_model

from radis.pgsearch.models import EmbeddingJob
from radis.pgsearch.tasks import embedding_launcher as _wrapped
from radis.reports.factories import ReportFactory

User = get_user_model()
embedding_launcher = _wrapped.__wrapped__  # type: ignore[attr-defined]
pytestmark = pytest.mark.django_db


def test_embedding_launcher_noop_when_job_in_flight():
    owner = User.objects.get(username="system")
    EmbeddingJob.objects.create(owner=owner, status=EmbeddingJob.Status.PREPARING)
    # Make a pending report so the second guard wouldn't short-circuit on its own.
    ReportFactory.create()

    with patch("radis.pgsearch.models.EmbeddingJob.delay") as delay_mock:
        embedding_launcher(context=None, timestamp=0)

    assert delay_mock.call_count == 0
    # No new job created.
    assert EmbeddingJob.objects.count() == 1


def test_embedding_launcher_noop_when_no_pending_rows():
    with patch("radis.pgsearch.models.EmbeddingJob.delay") as delay_mock:
        embedding_launcher(context=None, timestamp=0)

    assert delay_mock.call_count == 0
    assert EmbeddingJob.objects.count() == 0


def test_embedding_launcher_happy_path_creates_job_and_defers(
    django_capture_on_commit_callbacks,
):
    ReportFactory.create()

    with patch("radis.pgsearch.models.EmbeddingJob.delay") as delay_mock:
        with django_capture_on_commit_callbacks(execute=True):
            embedding_launcher(context=None, timestamp=0)

    assert EmbeddingJob.objects.count() == 1
    job = EmbeddingJob.objects.get()
    assert job.status == EmbeddingJob.Status.PREPARING
    assert job.owner.username == "system"
    delay_mock.assert_called_once()
