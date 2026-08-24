"""Tests for the `embed_pending` management command."""

import json
import logging
from io import StringIO
from unittest.mock import patch

import pytest
from django.conf import settings
from django.core.management import CommandError, call_command
from django.db import connection

from radis.core.utils.model_spec import parse_model_spec
from radis.pgsearch.models import EmbeddingBackfillRun
from radis.reports.factories import ReportFactory

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def _embedding_configured(settings):
    """The command refuses to run when no embedding model is configured.
    Default the whole module to a configured model; the no-model test blanks it."""
    settings.EMBEDDINGS_MODEL = parse_model_spec("qwen3")


def test_embed_pending_errors_when_model_not_configured(settings):
    settings.EMBEDDINGS_MODEL = None
    ReportFactory.create()
    with pytest.raises(CommandError, match="EMBEDDINGS_MODEL"):
        call_command("embed_pending")
    assert EmbeddingBackfillRun.objects.count() == 0


def test_nothing_to_embed():
    out = StringIO()
    with patch("radis.pgsearch.management.commands.embed_pending.enqueue_embed_reports") as enqueue:
        call_command("embed_pending", stdout=out)
    assert "Nothing to embed." in out.getvalue()
    enqueue.assert_not_called()


def test_enqueues_via_helper_with_explicit_subjob_size():
    # ReportFactory triggers the FTS post_save signal → ReportSearchIndex row with embedding=NULL.
    reports = [ReportFactory.create() for _ in range(5)]
    expected_ids = sorted(r.pk for r in reports)

    out = StringIO()
    with patch(
        "radis.pgsearch.management.commands.embed_pending.enqueue_embed_reports",
        return_value=3,
    ) as enqueue:
        call_command("embed_pending", "--subjob-size", "2", stdout=out)

    # The command delegates chunking to the shared helper and tags the
    # subjobs with BACKFILL priority so they can't starve subsequent live
    # ingest writes that land on the embeddings queue while a long
    # backfill is draining.
    enqueue.assert_called_once()
    args, kwargs = enqueue.call_args
    assert sorted(args[0]) == expected_ids
    assert kwargs["subjob_size"] == 2
    assert kwargs["priority"] == settings.EMBEDDINGS_BACKFILL_PRIORITY

    output = out.getvalue()
    assert "5 report(s) in subjobs of 2" in output
    assert "Deferred 3 subjob(s)" in output


def test_limit_caps_work():
    [ReportFactory.create() for _ in range(5)]

    out = StringIO()
    with patch(
        "radis.pgsearch.management.commands.embed_pending.enqueue_embed_reports",
        return_value=1,
    ) as enqueue:
        call_command("embed_pending", "--limit", "3", "--subjob-size", "10", stdout=out)

    args, kwargs = enqueue.call_args
    assert len(args[0]) == 3
    assert kwargs["subjob_size"] == 10
    assert kwargs["priority"] == settings.EMBEDDINGS_BACKFILL_PRIORITY


def test_logs_info_at_invoke_and_done(caplog):
    cmd_logger = logging.getLogger("radis.pgsearch.management.commands.embed_pending")
    cmd_logger.addHandler(caplog.handler)
    caplog.set_level(logging.INFO, logger=cmd_logger.name)
    try:
        [ReportFactory.create() for _ in range(2)]
        out = StringIO()
        with patch(
            "radis.pgsearch.management.commands.embed_pending.enqueue_embed_reports",
            return_value=1,
        ):
            call_command("embed_pending", "--subjob-size", "5", stdout=out)
    finally:
        cmd_logger.removeHandler(caplog.handler)

    info_msgs = [r.getMessage() for r in caplog.records if r.levelname == "INFO"]
    assert any("embed_pending: command invoked; subjob_size=5 limit=None" in m for m in info_msgs)
    assert any("embed_pending: done; reports=2 subjobs=1" in m for m in info_msgs)


@pytest.fixture
def insert_live_job_for_run():
    """Insert a live (todo) embed_reports_task row carrying `run.pk`, via
    raw SQL because ProcrastinateJob's Django ORM surface is read-only —
    same pattern as `_insert_embed_job` in test_backfill_run.py."""

    def _insert(run: EmbeddingBackfillRun) -> None:
        with connection.cursor() as cur:
            cur.execute(
                "INSERT INTO procrastinate_jobs "
                "(queue_name, task_name, priority, lock, queueing_lock, args, status, attempts) "
                "VALUES ('embeddings', 'radis.pgsearch.tasks.embed_reports_task', 0, NULL, NULL, "
                "%s, 'todo'::procrastinate_job_status, 0)",
                [json.dumps({"report_ids": [1], "run_id": run.pk})],
            )

    return _insert


def test_embed_pending_creates_run_with_enqueued_total():
    [ReportFactory.create() for _ in range(3)]
    call_command("embed_pending")
    run = EmbeddingBackfillRun.objects.get()
    assert run.total_reports == 3
    assert run.is_active
    assert run.triggered_by == "embed_pending"


def test_embed_pending_refuses_while_backfill_active(insert_live_job_for_run):
    active = EmbeddingBackfillRun.objects.create(total_reports=10, triggered_by="first")
    insert_live_job_for_run(active)
    ReportFactory.create()
    with pytest.raises(CommandError, match="already active"):
        call_command("embed_pending")
    assert EmbeddingBackfillRun.objects.count() == 1


def test_embed_pending_supersedes_abandoned_run():
    EmbeddingBackfillRun.objects.create(total_reports=10, triggered_by="first")
    ReportFactory.create()
    call_command("embed_pending")
    assert EmbeddingBackfillRun.objects.filter(cancelled_at__isnull=False).count() == 1
    active = EmbeddingBackfillRun.get_active()
    assert active is not None
    assert active.triggered_by == "embed_pending"
