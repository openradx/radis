"""Tests for the ReportSearchIndex admin pipeline-stats badge."""
from django.db import connection

import pytest

from radis.pgsearch.admin import ReportSearchIndexAdmin
from radis.pgsearch.models import ReportSearchIndex
from radis.reports.factories import ReportFactory

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.fixture(autouse=True)
def _clear_procrastinate_jobs():
    """ProcrastinateJob is read-only via the ORM, so pytest-django's
    flush between transactional tests doesn't clear it. Truncate
    explicitly so each test starts from an empty queue."""
    with connection.cursor() as cur:
        cur.execute("TRUNCATE procrastinate_jobs RESTART IDENTITY CASCADE")
    yield
    with connection.cursor() as cur:
        cur.execute("TRUNCATE procrastinate_jobs RESTART IDENTITY CASCADE")


def _insert_procrastinate_job(status: str, queue: str = "embeddings") -> None:
    """Insert a row directly via SQL because ProcrastinateJob's Django ORM
    surface is intentionally read-only — Procrastinate owns writes. We
    only need (queue_name, status) for the stats helper to count."""
    with connection.cursor() as cur:
        cur.execute(
            "INSERT INTO procrastinate_jobs "
            "(queue_name, task_name, priority, lock, queueing_lock, args, status, attempts) "
            "VALUES (%s, %s, %s, NULL, NULL, %s, %s::procrastinate_job_status, %s)",
            [
                queue,
                "radis.pgsearch.tasks.embed_reports_task",
                0,
                '{"report_ids": []}',
                status,
                0,
            ],
        )


def test_pipeline_stats_counts_pending_rsvs():
    [ReportFactory.create() for _ in range(3)]
    embedded = ReportFactory.create()
    rsv = ReportSearchIndex.objects.get(report_id=embedded.pk)
    rsv.embedding = [0.0] * 1024
    rsv.save()

    stats = ReportSearchIndexAdmin._embedding_pipeline_stats()
    assert stats["pending_reports"] == 3


def test_pipeline_stats_counts_procrastinate_jobs_by_status():
    _insert_procrastinate_job("todo")
    _insert_procrastinate_job("todo")
    _insert_procrastinate_job("doing")
    _insert_procrastinate_job("failed")
    # Job on a different queue must not be counted.
    _insert_procrastinate_job("todo", queue="default")

    stats = ReportSearchIndexAdmin._embedding_pipeline_stats()
    assert stats["todo"] == 2
    assert stats["doing"] == 1
    assert stats["failed"] == 1


def test_pipeline_stats_zero_when_no_queue_activity():
    stats = ReportSearchIndexAdmin._embedding_pipeline_stats()
    assert stats == {
        "pending_reports": 0,
        "todo": 0,
        "doing": 0,
        "failed": 0,
    }
