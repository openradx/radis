"""Tests for the ReportSearchIndex admin pipeline-stats badge."""

import logging
from unittest.mock import MagicMock, patch

import pytest
from django.conf import settings
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


def _insert_procrastinate_job(status: str, queue: str = "embeddings", priority: int = 0) -> None:
    """Insert a row directly via SQL because ProcrastinateJob's Django ORM
    surface is intentionally read-only — Procrastinate owns writes. We
    only need (queue_name, status, priority) for the stats helper to count."""
    with connection.cursor() as cur:
        cur.execute(
            "INSERT INTO procrastinate_jobs "
            "(queue_name, task_name, priority, lock, queueing_lock, args, status, attempts) "
            "VALUES (%s, %s, %s, NULL, NULL, %s, %s::procrastinate_job_status, %s)",
            [
                queue,
                "radis.pgsearch.tasks.embed_reports_task",
                priority,
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
    # Live-priority job counts as todo but not as cancellable backfill.
    _insert_procrastinate_job("todo", priority=settings.EMBEDDING_LIVE_PRIORITY)

    stats = ReportSearchIndexAdmin._embedding_pipeline_stats()
    assert stats["todo"] == 3
    assert stats["todo_backfill"] == 2
    assert stats["doing"] == 1
    assert stats["failed"] == 1


def test_pipeline_stats_zero_when_no_queue_activity():
    stats = ReportSearchIndexAdmin._embedding_pipeline_stats()
    assert stats == {
        "pending_reports": 0,
        "todo": 0,
        "todo_backfill": 0,
        "doing": 0,
        "failed": 0,
    }


def test_delete_permission_denied():
    """RSI rows are managed by the post_save signal on Report — admin must
    not let operators delete them out from under the model."""
    admin_instance = ReportSearchIndexAdmin(ReportSearchIndex, AdminSite())
    assert admin_instance.has_delete_permission(MagicMock()) is False


def test_clear_embeddings_nulls_only_selected_rows_with_embeddings():
    """NULL the existing embeddings on selected rows. Rows already NULL
    are no-ops; rows outside the selection are untouched."""
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
    admin_instance.clear_embeddings(MagicMock(), selected)

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


def test_enqueue_pending_embeddings_logs_info_with_user_and_counts(caplog):
    admin_logger = logging.getLogger("radis.pgsearch.admin")
    admin_logger.addHandler(caplog.handler)
    caplog.set_level(logging.INFO, logger="radis.pgsearch.admin")
    try:
        targets = [ReportFactory.create() for _ in range(2)]
        selected = ReportSearchIndex.objects.filter(report_id__in=[r.pk for r in targets])
        request = MagicMock()
        request.user.get_username.return_value = "alice"

        admin_instance = ReportSearchIndexAdmin(ReportSearchIndex, AdminSite())
        admin_instance.message_user = MagicMock()
        with patch("radis.pgsearch.admin.enqueue_embed_reports", return_value=1):
            admin_instance.enqueue_pending_embeddings(request, selected)
    finally:
        admin_logger.removeHandler(caplog.handler)

    info_msgs = [r.getMessage() for r in caplog.records if r.levelname == "INFO"]
    assert any(
        "admin.enqueue_pending_embeddings: user=alice enqueued 2 report(s) across 1 subjob(s)" in m
        for m in info_msgs
    )


def test_clear_embeddings_logs_info_with_user_and_count(caplog):
    admin_logger = logging.getLogger("radis.pgsearch.admin")
    admin_logger.addHandler(caplog.handler)
    caplog.set_level(logging.INFO, logger="radis.pgsearch.admin")
    try:
        targets = [ReportFactory.create() for _ in range(2)]
        for r in targets:
            rsi = ReportSearchIndex.objects.get(report_id=r.pk)
            rsi.embedding = [0.1] * 1024
            rsi.save()
        selected = ReportSearchIndex.objects.filter(report_id__in=[r.pk for r in targets])
        request = MagicMock()
        request.user.get_username.return_value = "bob"

        admin_instance = ReportSearchIndexAdmin(ReportSearchIndex, AdminSite())
        admin_instance.message_user = MagicMock()
        admin_instance.clear_embeddings(request, selected)
    finally:
        admin_logger.removeHandler(caplog.handler)

    info_msgs = [r.getMessage() for r in caplog.records if r.levelname == "INFO"]
    assert any("admin.clear_embeddings: user=bob cleared 2 embedding(s)" in m for m in info_msgs)


def test_cancel_backfill_url_is_registered():
    from django.urls import reverse

    assert reverse("admin:pgsearch_reportsearchindex_cancel_backfill").endswith("/cancel-backfill/")


def test_cancel_backfill_view_cancels_and_redirects(settings):
    from procrastinate.contrib.django.models import ProcrastinateJob

    from radis.pgsearch import tasks as tasks_module

    tasks_module.enqueue_embed_reports(
        [1, 2], subjob_size=1, priority=settings.EMBEDDING_BACKFILL_PRIORITY
    )

    admin_instance = ReportSearchIndexAdmin(ReportSearchIndex, AdminSite())
    admin_instance.message_user = MagicMock()
    request = MagicMock()
    request.method = "POST"
    request.user.get_username.return_value = "alice"

    response = admin_instance.cancel_backfill_view(request)

    assert response.status_code == 302
    assert ProcrastinateJob.objects.filter(status="cancelled").count() == 2
    msg = admin_instance.message_user.call_args.args[1]
    assert "Cancelled 2 queued backfill subjob(s)" in msg


def test_cancel_backfill_view_warns_when_nothing_queued():
    from django.contrib import messages

    admin_instance = ReportSearchIndexAdmin(ReportSearchIndex, AdminSite())
    admin_instance.message_user = MagicMock()
    request = MagicMock()
    request.method = "POST"

    response = admin_instance.cancel_backfill_view(request)

    assert response.status_code == 302
    call = admin_instance.message_user.call_args
    assert "No queued backfill subjobs to cancel." in call.args[1]
    assert call.kwargs.get("level") == messages.WARNING


def test_cancel_backfill_view_rejects_get():
    admin_instance = ReportSearchIndexAdmin(ReportSearchIndex, AdminSite())
    request = MagicMock()
    request.method = "GET"

    response = admin_instance.cancel_backfill_view(request)

    assert response.status_code == 405


def test_cancel_backfill_view_requires_change_permission():
    from django.core.exceptions import PermissionDenied

    admin_instance = ReportSearchIndexAdmin(ReportSearchIndex, AdminSite())
    request = MagicMock()
    request.method = "POST"
    request.user.has_perm.return_value = False

    with pytest.raises(PermissionDenied):
        admin_instance.cancel_backfill_view(request)
