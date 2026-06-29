"""Tests for the ReportSearchIndex admin pipeline-stats badge."""

from unittest.mock import MagicMock

import pytest
from django.contrib.admin.sites import AdminSite
from django.db import connection

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


def test_delete_permission_denied():
    """RSI rows are managed by the post_save signal on Report — admin must
    not let operators delete them out from under the model."""
    admin_instance = ReportSearchIndexAdmin(ReportSearchIndex, AdminSite())
    assert admin_instance.has_delete_permission(MagicMock()) is False


def test_clear_embeddings_for_remodel_nulls_only_selected_rows_with_embeddings():
    """Same-dim model swap: NULL the existing embeddings on selected rows.
    Rows already NULL are no-ops; rows outside the selection are untouched."""
    targets = [ReportFactory.create() for _ in range(3)]
    untouched = ReportFactory.create()
    for r in targets + [untouched]:
        rsi = ReportSearchIndex.objects.get(report_id=r.pk)
        rsi.embedding = [0.1] * 1024
        rsi.save()
    # One target already NULL — should be skipped by the filter.
    ReportSearchIndex.objects.filter(report_id=targets[0].pk).update(embedding=None)

    selected = ReportSearchIndex.objects.filter(report_id__in=[r.pk for r in targets])
    admin_instance = ReportSearchIndexAdmin(ReportSearchIndex, AdminSite())
    admin_instance.message_user = MagicMock()
    admin_instance.clear_embeddings_for_remodel(MagicMock(), selected)

    # Two of three targets had embeddings and got cleared.
    assert (
        ReportSearchIndex.objects.filter(
            report_id__in=[r.pk for r in targets], embedding__isnull=True
        ).count()
        == 3
    )
    # The non-selected row is untouched.
    assert ReportSearchIndex.objects.get(report_id=untouched.pk).embedding is not None
    # message_user reports the number cleared, not the number selected.
    msg_args = admin_instance.message_user.call_args
    assert "Cleared embeddings on 2 row(s)" in msg_args.args[1]
