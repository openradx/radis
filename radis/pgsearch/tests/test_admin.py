"""Tests for the ReportSearchIndex admin pipeline-stats badge."""

import json
import logging
import re
from unittest.mock import MagicMock, patch

import pytest
from django.conf import settings
from django.contrib import messages
from django.contrib.admin.sites import AdminSite
from django.db import connection
from django.test import Client
from django.urls import reverse
from django.utils import timezone
from procrastinate.contrib.django.models import ProcrastinateJob

from radis.core.utils.model_spec import parse_model_spec
from radis.pgsearch.admin import ReportSearchIndexAdmin
from radis.pgsearch.models import EmbeddingBackfillRun, ReportSearchIndex
from radis.reports.factories import ReportFactory

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.fixture(autouse=True)
def _embedding_model_configured(settings):
    """The enqueue-pending admin action no-ops when no embedding model is
    configured, so default these tests to a configured model."""
    settings.EMBEDDINGS_MODEL = parse_model_spec("qwen3")


@pytest.fixture
def admin_client() -> Client:
    """Create an authenticated admin client for testing the admin interface."""
    from adit_radis_shared.accounts.factories import AdminUserFactory

    client = Client()
    admin_user = AdminUserFactory.create()
    client.force_login(admin_user)
    return client


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


def _insert_procrastinate_job(
    status: str,
    queue: str = "embeddings",
    priority: int = 0,
    report_ids: list[int] | None = None,
    args_json: str | None = None,
) -> None:
    """Insert a row directly via SQL because ProcrastinateJob's Django ORM
    surface is intentionally read-only — Procrastinate owns writes. The
    stats helper reads (queue_name, status, priority) and sums
    jsonb_array_length(args->'report_ids'); `args_json` overrides the args
    payload entirely (e.g. '{}' for a job without report_ids)."""
    if args_json is None:
        args_json = json.dumps({"report_ids": report_ids or []})
    with connection.cursor() as cur:
        cur.execute(
            "INSERT INTO procrastinate_jobs "
            "(queue_name, task_name, priority, lock, queueing_lock, args, status, attempts) "
            "VALUES (%s, %s, %s, NULL, NULL, %s, %s::procrastinate_job_status, %s)",
            [
                queue,
                "radis.pgsearch.tasks.embed_reports_task",
                priority,
                args_json,
                status,
                0,
            ],
        )


def _squash_ws(html: str) -> str:
    """Template newlines/indentation render as whitespace runs; collapse
    them so assertions can match across djlint's line wrapping."""
    return re.sub(r"\s+", " ", html)


def test_pipeline_stats_counts_pending_rsvs():
    [ReportFactory.create() for _ in range(3)]
    embedded = ReportFactory.create()
    rsv = ReportSearchIndex.objects.get(report_id=embedded.pk)
    rsv.embedding = [0.0] * 1024
    rsv.save()

    stats = ReportSearchIndexAdmin._embedding_pipeline_stats()
    assert stats["pending_reports"] == 3


def test_pipeline_stats_counts_procrastinate_jobs_by_status():
    run = EmbeddingBackfillRun.objects.create(total_reports=10, triggered_by="t")
    _insert_procrastinate_job("todo", args_json=json.dumps({"report_ids": [], "run_id": run.pk}))
    _insert_procrastinate_job("todo", args_json=json.dumps({"report_ids": [], "run_id": run.pk}))
    _insert_procrastinate_job("doing")
    _insert_procrastinate_job("failed")
    # Job on a different queue must not be counted.
    _insert_procrastinate_job("todo", queue="default")
    # Todo job carrying no run_id (write-path) counts as todo but not as
    # cancellable backfill — cancel is run-scoped, not priority-scoped.
    _insert_procrastinate_job("todo", priority=settings.EMBEDDINGS_LIVE_PRIORITY)

    stats = ReportSearchIndexAdmin._embedding_pipeline_stats()
    assert stats["todo"] == 3
    assert stats["todo_backfill"] == 2
    assert stats["doing"] == 1
    assert stats["failed"] == 1


def test_pipeline_stats_todo_backfill_scoped_to_active_run():
    """todo_backfill counts only todo jobs whose run_id belongs to the
    active run — not a since-closed run's leftover jobs, not write-path
    jobs (no run_id), and it does count a job re-prioritized above
    backfill priority (simulating retry_stalled_jobs) because the
    selection is run-scoped, not priority-scoped."""
    active = EmbeddingBackfillRun.objects.create(total_reports=5, triggered_by="t")
    closed = EmbeddingBackfillRun.objects.create(
        total_reports=3, triggered_by="t2", cancelled_at=timezone.now()
    )
    _insert_procrastinate_job(
        "todo", args_json=json.dumps({"report_ids": [1], "run_id": active.pk})
    )
    _insert_procrastinate_job(
        "todo",
        priority=10,
        args_json=json.dumps({"report_ids": [2], "run_id": active.pk}),
    )
    _insert_procrastinate_job(
        "todo", args_json=json.dumps({"report_ids": [3], "run_id": closed.pk})
    )
    _insert_procrastinate_job("todo", args_json=json.dumps({"report_ids": [4]}))

    stats = ReportSearchIndexAdmin._embedding_pipeline_stats()
    assert stats["todo_backfill"] == 2


def test_pipeline_stats_sums_reports_per_status():
    _insert_procrastinate_job("todo", report_ids=[1, 2])
    _insert_procrastinate_job("todo", report_ids=[3, 4, 5])
    _insert_procrastinate_job("doing", report_ids=[6, 7, 8, 9])
    # Failed jobs stay a bare subjob count — their reports are not summed
    # into either report total.
    _insert_procrastinate_job("failed", report_ids=[10])
    # Job on a different queue must not be counted.
    _insert_procrastinate_job("todo", queue="default", report_ids=[11, 12])

    stats = ReportSearchIndexAdmin._embedding_pipeline_stats()
    assert stats["todo"] == 2
    assert stats["todo_reports"] == 5
    assert stats["doing"] == 1
    assert stats["doing_reports"] == 4
    assert stats["failed"] == 1


def test_pipeline_stats_tolerates_jobs_without_report_ids():
    """A job whose args lack report_ids contributes NULL to the sum, which
    Sum() skips — the subjob is still counted, the report total isn't."""
    _insert_procrastinate_job("todo", report_ids=[1, 2, 3])
    _insert_procrastinate_job("todo", args_json="{}")

    stats = ReportSearchIndexAdmin._embedding_pipeline_stats()
    assert stats["todo"] == 2
    assert stats["todo_reports"] == 3


def test_pipeline_stats_zero_when_no_queue_activity():
    stats = ReportSearchIndexAdmin._embedding_pipeline_stats()
    assert stats == {
        "total_reports": 0,
        "embedded_reports": 0,
        "pending_reports": 0,
        "todo": 0,
        "todo_reports": 0,
        "todo_backfill": 0,
        "doing": 0,
        "doing_reports": 0,
        "unqueued_reports": 0,
        "failed": 0,
        "run": None,
        "run_stalled": False,
    }


def test_pipeline_stats_report_centric_keys():
    pending = [ReportFactory.create() for _ in range(4)]
    embedded = ReportFactory.create()
    rsi = ReportSearchIndex.objects.get(report_id=embedded.pk)
    rsi.embedding = [0.0] * 1024
    rsi.save()
    _insert_procrastinate_job("todo", report_ids=[pending[0].pk, pending[1].pk])
    _insert_procrastinate_job("doing", report_ids=[pending[2].pk])

    stats = ReportSearchIndexAdmin._embedding_pipeline_stats()
    assert stats["total_reports"] == 5
    assert stats["embedded_reports"] == 1
    assert stats["pending_reports"] == 4
    assert stats["unqueued_reports"] == 1  # 4 pending - 2 queued - 1 doing


def test_pipeline_stats_unqueued_clamped_at_zero():
    [ReportFactory.create() for _ in range(1)]
    _insert_procrastinate_job("todo", report_ids=[1, 2, 3])  # covers more than pending
    stats = ReportSearchIndexAdmin._embedding_pipeline_stats()
    assert stats["unqueued_reports"] == 0


def test_pipeline_stats_active_run_and_stall_flag():
    run = EmbeddingBackfillRun.objects.create(total_reports=10, triggered_by="t")
    stats = ReportSearchIndexAdmin._embedding_pipeline_stats()
    assert stats["run"] == run
    assert stats["run_stalled"] is True  # unfinished, no live subjobs

    import json as _json

    _insert_procrastinate_job("todo", args_json=_json.dumps({"report_ids": [1], "run_id": run.pk}))
    stats = ReportSearchIndexAdmin._embedding_pipeline_stats()
    assert stats["run_stalled"] is False


def test_changelist_badge_report_centric_primary_line(admin_client):
    pending = [ReportFactory.create() for _ in range(3)]
    done = ReportFactory.create()
    rsi = ReportSearchIndex.objects.get(report_id=done.pk)
    rsi.embedding = [0.0] * 1024
    rsi.save()
    _insert_procrastinate_job("todo", report_ids=[pending[0].pk, pending[1].pk])
    _insert_procrastinate_job("doing", report_ids=[pending[2].pk])

    url = reverse("admin:pgsearch_reportsearchindex_changelist")
    html = _squash_ws(admin_client.get(url).content.decode())

    assert "<strong>1</strong> / <strong>4</strong> reports processed" in html
    assert "2 queued" in html
    assert "1 in progress" in html
    assert "not queued" not in html  # zero segment omitted
    assert "subjobs: 1 queued · 1 in-flight · 0 failed" in html


def test_changelist_badge_idle_state_is_bare_fraction(admin_client):
    done = ReportFactory.create()
    rsi = ReportSearchIndex.objects.get(report_id=done.pk)
    rsi.embedding = [0.0] * 1024
    rsi.save()

    url = reverse("admin:pgsearch_reportsearchindex_changelist")
    html = _squash_ws(admin_client.get(url).content.decode())

    assert "<strong>1</strong> / <strong>1</strong> reports processed" in html
    assert "queued" not in html
    assert "in progress" not in html
    assert "subjobs:" not in html
    assert "Backfill:" not in html


def test_changelist_badge_backfill_line_with_stall_marker(admin_client):
    EmbeddingBackfillRun.objects.create(total_reports=8, processed_reports=2, triggered_by="t")
    url = reverse("admin:pgsearch_reportsearchindex_changelist")
    html = _squash_ws(admin_client.get(url).content.decode())

    assert "Backfill: <strong>2</strong> / <strong>8</strong> reports processed (25%)" in html
    assert "stalled — no live subjobs" in html


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

    run = EmbeddingBackfillRun.objects.create(total_reports=2, triggered_by="alice")
    tasks_module.enqueue_embed_reports(
        [1, 2], subjob_size=1, priority=settings.EMBEDDINGS_BACKFILL_PRIORITY, run_id=run.pk
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


def test_enqueue_pending_embeddings_creates_run_and_threads_run_id():
    targets = [ReportFactory.create() for _ in range(2)]
    selected = ReportSearchIndex.objects.filter(report_id__in=[r.pk for r in targets])
    admin_instance = ReportSearchIndexAdmin(ReportSearchIndex, AdminSite())
    admin_instance.message_user = MagicMock()
    request = MagicMock()
    request.user.get_username.return_value = "alice"

    admin_instance.enqueue_pending_embeddings(request, selected)

    run = EmbeddingBackfillRun.objects.get()
    assert run.total_reports == 2
    assert run.triggered_by == "alice"
    job_args = ProcrastinateJob.objects.filter(queue_name="embeddings").values_list(
        "args", flat=True
    )
    assert all(args.get("run_id") == run.pk for args in job_args)


def test_enqueue_pending_embeddings_warns_while_backfill_active():
    active = EmbeddingBackfillRun.objects.create(total_reports=10, triggered_by="first")
    # An args payload carrying the active run's id makes the guard see a live
    # subjob (the helper's default args carry no run_id, so it won't count):
    _insert_procrastinate_job(
        "todo", args_json=json.dumps({"report_ids": [1], "run_id": active.pk})
    )
    target = ReportFactory.create()
    selected = ReportSearchIndex.objects.filter(report_id=target.pk)
    admin_instance = ReportSearchIndexAdmin(ReportSearchIndex, AdminSite())
    admin_instance.message_user = MagicMock()
    request = MagicMock()
    request.user.get_username.return_value = "bob"

    admin_instance.enqueue_pending_embeddings(request, selected)

    assert EmbeddingBackfillRun.objects.count() == 1  # no new run
    call = admin_instance.message_user.call_args
    assert "already active" in call.args[1]
    assert call.kwargs.get("level") == messages.WARNING


def test_enqueue_pending_embeddings_warns_when_model_not_configured(settings):
    """The other three EMBEDDINGS_MODEL-is-None guards are covered at
    test_embed_pending_command.py, test_embed_reports_task.py, and
    test_provider_hybrid.py — this is the admin action's own no-model path:
    it must not create a run or enqueue any subjob, and must surface a
    warning naming the setting the operator needs to set."""
    settings.EMBEDDINGS_MODEL = None
    target = ReportFactory.create()
    selected = ReportSearchIndex.objects.filter(report_id=target.pk)
    admin_instance = ReportSearchIndexAdmin(ReportSearchIndex, AdminSite())
    admin_instance.message_user = MagicMock()
    request = MagicMock()
    request.user.get_username.return_value = "alice"

    with patch("radis.pgsearch.admin.enqueue_embed_reports") as enqueue:
        admin_instance.enqueue_pending_embeddings(request, selected)

    enqueue.assert_not_called()
    assert EmbeddingBackfillRun.objects.count() == 0
    call = admin_instance.message_user.call_args
    assert "EMBEDDINGS_MODEL" in call.args[1]
    assert call.kwargs.get("level") == messages.WARNING
