"""Tests for the EmbeddingBackfillRun model (spec §6.8)."""

import json

import pytest
from django.db import connection
from django.utils import timezone

from radis.pgsearch.models import EmbeddingBackfillRun
from radis.pgsearch.tasks import (
    ActiveBackfillError,
    cancel_backfill_embeddings,
    create_backfill_run,
)

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.fixture(autouse=True)
def _clear_procrastinate_jobs():
    """ProcrastinateJob is read-only via the ORM, so pytest-django's flush
    between transactional tests doesn't clear it. Truncate explicitly."""
    with connection.cursor() as cur:
        cur.execute("TRUNCATE procrastinate_jobs RESTART IDENTITY CASCADE")
    yield
    with connection.cursor() as cur:
        cur.execute("TRUNCATE procrastinate_jobs RESTART IDENTITY CASCADE")


def _insert_embed_job(status: str, run_id: int | None, report_ids: list[int] | None = None) -> None:
    args = {"report_ids": report_ids or []}
    if run_id is not None:
        args["run_id"] = run_id  # type: ignore[typeddict-unknown-key]
    with connection.cursor() as cur:
        cur.execute(
            "INSERT INTO procrastinate_jobs "
            "(queue_name, task_name, priority, lock, queueing_lock, args, status, attempts) "
            "VALUES ('embeddings', 'radis.pgsearch.tasks.embed_reports_task', 0, NULL, NULL, "
            "%s, %s::procrastinate_job_status, 0)",
            [json.dumps(args), status],
        )


def test_is_active_semantics():
    run = EmbeddingBackfillRun.objects.create(total_reports=10, triggered_by="test")
    assert run.is_active
    run.finished_at = timezone.now()
    assert not run.is_active
    run.finished_at = None
    run.cancelled_at = timezone.now()
    assert not run.is_active


def test_get_active_returns_latest_active_or_none():
    assert EmbeddingBackfillRun.get_active() is None
    EmbeddingBackfillRun.objects.create(
        total_reports=5, triggered_by="old", finished_at=timezone.now()
    )
    active = EmbeddingBackfillRun.objects.create(total_reports=7, triggered_by="live")
    assert EmbeddingBackfillRun.get_active() == active


def test_live_subjob_count_scopes_to_run_and_live_statuses():
    run = EmbeddingBackfillRun.objects.create(total_reports=10, triggered_by="test")
    other = EmbeddingBackfillRun.objects.create(
        total_reports=3, triggered_by="other", cancelled_at=timezone.now()
    )
    _insert_embed_job("todo", run_id=run.pk, report_ids=[1, 2])
    _insert_embed_job("doing", run_id=run.pk, report_ids=[3])
    _insert_embed_job("failed", run_id=run.pk, report_ids=[4])  # not live
    _insert_embed_job("todo", run_id=other.pk, report_ids=[5])  # other run
    _insert_embed_job("todo", run_id=None, report_ids=[6])  # write-path job
    assert run.live_subjob_count() == 2


def test_create_backfill_run_refuses_while_active_with_live_subjobs():
    active = EmbeddingBackfillRun.objects.create(total_reports=10, triggered_by="first")
    _insert_embed_job("todo", run_id=active.pk, report_ids=[1, 2])
    with pytest.raises(ActiveBackfillError, match="already active"):
        create_backfill_run(5, triggered_by="second")
    assert EmbeddingBackfillRun.objects.count() == 1


def test_create_backfill_run_auto_closes_abandoned_run():
    abandoned = EmbeddingBackfillRun.objects.create(total_reports=10, triggered_by="first")
    # no live jobs for `abandoned` -> it is auto-closed and superseded
    run = create_backfill_run(5, triggered_by="second")
    abandoned.refresh_from_db()
    assert abandoned.cancelled_at is not None
    assert run.is_active
    assert run.total_reports == 5
    assert EmbeddingBackfillRun.get_active() == run


def test_cancel_backfill_embeddings_stamps_active_runs():
    run = EmbeddingBackfillRun.objects.create(total_reports=10, triggered_by="test")
    cancel_backfill_embeddings()
    run.refresh_from_db()
    assert run.cancelled_at is not None
