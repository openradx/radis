"""Tests for `embed_reports_task` and its chaining from `bulk_index_reports`."""

import logging
from contextlib import contextmanager
from unittest.mock import MagicMock, patch

import httpx
import numpy as np
import openai
import pytest
from django.utils import timezone

from radis.pgsearch.models import EmbeddingBackfillRun, ReportSearchIndex
from radis.pgsearch.tasks import (
    bulk_index_reports,
    embed_reports_task,
    enqueue_embed_reports,
)
from radis.pgsearch.utils.embedding_client import EmbeddingClientError
from radis.reports.factories import ReportFactory


@pytest.fixture
def no_retry_sleep(monkeypatch):
    """No-op the transient-retry backoff sleeps (with_transient_retries uses
    time.sleep) so tests that exercise retries don't slow the suite down."""
    import time

    monkeypatch.setattr(time, "sleep", lambda s: None)


@pytest.fixture
def caplog_tasks(caplog):
    """Attach caplog's handler to `radis.pgsearch.tasks` directly.

    The `radis` logger has `propagate=False` in settings, so caplog's
    root handler doesn't see records emitted under it. Captures from
    DEBUG upward — tests should filter `caplog.records` by `levelname`
    when asserting. Yields `caplog` so tests can assert on
    `caplog.records`."""
    task_logger = logging.getLogger("radis.pgsearch.tasks")
    task_logger.addHandler(caplog.handler)
    caplog.set_level(logging.DEBUG, logger="radis.pgsearch.tasks")
    try:
        yield caplog
    finally:
        task_logger.removeHandler(caplog.handler)


@pytest.fixture(autouse=True)
def _bypass_rate_limit_gate(monkeypatch):
    """These tests exercise embed_reports_task's business logic (retry,
    logging), not the rate-limit gate itself — that's covered in
    radis/core/tests/test_rate_limit.py. Patch the gate runner to a
    passthrough so a stray 429 in a test double can't trigger real sleeps,
    and reset the process-global gate so a 429 armed by one test can't
    leak a closed window into another."""
    from radis.pgsearch import tasks as tasks_module
    from radis.pgsearch.utils.embedding_client import EMBEDDING_GATE

    EMBEDDING_GATE.reset()
    monkeypatch.setattr(tasks_module, "run_through_gate", lambda gate, budget, fn: fn())


pytestmark = pytest.mark.django_db(transaction=True)


def _unit_vec(dim: int) -> list[float]:
    v = np.ones(dim, dtype=np.float32)
    return (v / np.linalg.norm(v)).tolist()


def _timeout_error() -> openai.APITimeoutError:
    """This backend has no explicit 'batch too large' rejection (confirmed
    empirically against the real embedding gateway — it accepts arbitrarily
    large batches and just gets proportionally slower), so an oversized
    chunk surfaces as a timeout instead of an error response."""
    request = httpx.Request("POST", "https://embedding.example/v1/embeddings")
    return openai.APITimeoutError(request)


def _make_fake_client(vec: list[float]) -> MagicMock:
    """MagicMock that mimics `with EmbeddingClient() as c` and
    `c.embed_documents([...])`."""
    instance = MagicMock()
    instance.__enter__ = MagicMock(return_value=instance)
    instance.__exit__ = MagicMock(return_value=None)
    instance.embed_documents = MagicMock(side_effect=lambda texts: [vec] * len(texts))
    return instance


@contextmanager
def _mock_embedding_client(error: Exception | None = None):
    """Patch `EmbeddingClient` using this file's `_make_fake_client` mock
    style so `embed_documents` returns one unit vector per input text (or
    raises `error`)."""
    from django.conf import settings as dj_settings

    if error is not None:
        fake = MagicMock()
        fake.__enter__ = MagicMock(return_value=fake)
        fake.__exit__ = MagicMock(return_value=None)
        fake.embed_documents = MagicMock(side_effect=error)
    else:
        fake = _make_fake_client(_unit_vec(dj_settings.EMBEDDING_DIM))
    with patch("radis.pgsearch.tasks.EmbeddingClient", return_value=fake):
        yield fake


def test_empty_input_no_ops():
    with patch("radis.pgsearch.tasks.EmbeddingClient") as client_cls:
        embed_reports_task(report_ids=[])
    client_cls.assert_not_called()


def test_no_matching_rsvs_no_ops():
    """Report ids that don't resolve to ReportSearchIndex rows are a no-op — the task does
    not contact the embedding service."""
    with patch("radis.pgsearch.tasks.EmbeddingClient") as client_cls:
        embed_reports_task(report_ids=[999_999])
    client_cls.assert_not_called()


def test_logs_info_start_with_report_count(settings, caplog_tasks):
    reports = [ReportFactory.create() for _ in range(2)]
    pks = [r.pk for r in reports]
    vec = _unit_vec(settings.EMBEDDING_DIM)
    fake = _make_fake_client(vec)

    with patch("radis.pgsearch.tasks.EmbeddingClient", return_value=fake):
        embed_reports_task(report_ids=pks)

    info_msgs = [r.getMessage() for r in caplog_tasks.records if r.levelname == "INFO"]
    assert any("embed_reports_task: start; reports=2" in m for m in info_msgs)


def test_embeds_in_internal_batches(settings):
    settings.EMBEDDING_BATCH_SIZE = 2
    reports = [ReportFactory.create() for _ in range(5)]
    pks = [r.pk for r in reports]
    vec = _unit_vec(settings.EMBEDDING_DIM)
    fake = _make_fake_client(vec)

    with patch("radis.pgsearch.tasks.EmbeddingClient", return_value=fake):
        embed_reports_task(report_ids=pks)

    # 5 reports with batch_size=2 → 3 embed_documents calls of sizes 2, 2, 1.
    assert fake.embed_documents.call_count == 3
    sizes = [len(call.args[0]) for call in fake.embed_documents.call_args_list]
    assert sorted(sizes) == [1, 2, 2]
    assert ReportSearchIndex.objects.filter(embedding__isnull=True).count() == 0


def test_embed_chunk_with_retry_runs_through_gate_with_batch_budget(monkeypatch):
    from django.conf import settings

    from radis.pgsearch import tasks as tasks_module
    from radis.pgsearch.utils.embedding_client import EMBEDDING_GATE

    seen = {}

    def fake_run_through_gate(gate, budget, fn):
        seen["gate"] = gate
        seen["budget"] = budget
        return fn()

    monkeypatch.setattr(tasks_module, "run_through_gate", fake_run_through_gate)

    fake_client = MagicMock()
    fake_client.embed_documents = MagicMock(return_value=[[0.1, 0.2]])

    result = tasks_module._embed_chunk_with_retry(fake_client, ["hello"])

    assert result == [[0.1, 0.2]]
    assert seen["gate"] is EMBEDDING_GATE, "task embedding must use the shared embedding gate"
    assert seen["budget"] == settings.EMBEDDING_RATE_LIMIT_MAX_WAIT_SECONDS, (
        "background batches get the long batch budget, not the query budget"
    )
    fake_client.embed_documents.assert_called_once_with(["hello"])


def test_embedding_error_propagates():
    """Procrastinate retries depend on the exception escaping the task."""
    reports = [ReportFactory.create() for _ in range(2)]
    pks = [r.pk for r in reports]
    fake = MagicMock()
    fake.__enter__ = MagicMock(return_value=fake)
    fake.__exit__ = MagicMock(return_value=None)
    fake.embed_documents = MagicMock(side_effect=EmbeddingClientError("service down"))

    with patch("radis.pgsearch.tasks.EmbeddingClient", return_value=fake):
        with pytest.raises(EmbeddingClientError):
            embed_reports_task(report_ids=pks)

    assert ReportSearchIndex.objects.filter(embedding__isnull=True).count() == 2


def _defer_calls(cfg_mock):
    """Helper: return the (kwargs of) defer() calls made through the
    `app.configure_task` mock."""
    return [c.kwargs for c in cfg_mock.return_value.defer.call_args_list]


def test_bulk_index_reports_chains_into_embed_reports_task(settings):
    """`bulk_index_reports` upserts RSIs and then chunks the embed work via
    `enqueue_embed_reports`. The chain is the ordering guarantee: the
    embeddings worker only ever sees report ids whose RSI rows are already
    committed."""
    settings.EMBEDDING_SUBJOB_SIZE = 100
    reports = [ReportFactory.create() for _ in range(3)]
    pks = [r.pk for r in reports]
    ReportSearchIndex.objects.filter(report_id__in=pks).delete()

    with patch("radis.pgsearch.tasks.app.configure_task") as cfg:
        bulk_index_reports(report_ids=pks)

    # RSIs were upserted, then one embed subjob covering all 3 ids was
    # deferred (3 < SUBJOB_SIZE so the whole batch fits in one subjob).
    assert ReportSearchIndex.objects.filter(report_id__in=pks).count() == 3
    assert _defer_calls(cfg) == [{"report_ids": pks}]


def test_bulk_index_reports_splits_into_subjobs_when_exceeding_subjob_size(settings):
    """A bulk-upsert larger than `EMBEDDING_SUBJOB_SIZE` must defer multiple
    embed subjobs so the embeddings worker can drain them in parallel and
    retries/failures have bounded blast radius."""
    settings.EMBEDDING_SUBJOB_SIZE = 4
    reports = [ReportFactory.create() for _ in range(10)]
    pks = [r.pk for r in reports]

    with patch("radis.pgsearch.tasks.app.configure_task") as cfg:
        bulk_index_reports(report_ids=pks)

    # 10 reports / subjob 4 → 3 defer calls of sizes 4, 4, 2.
    enqueued_chunks = [c["report_ids"] for c in _defer_calls(cfg)]
    assert [len(c) for c in enqueued_chunks] == [4, 4, 2]
    # The union of all chunks covers exactly the input ids in order.
    assert [pk for c in enqueued_chunks for pk in c] == pks


def test_enqueue_embed_reports_helper_chunks_by_subjob_size(settings):
    """The shared `enqueue_embed_reports` helper is the single chunking
    point. A 1M-row backfill becomes ~10k subjobs (no single huge task);
    a single create with one id becomes one subjob (no overhead)."""
    settings.EMBEDDING_SUBJOB_SIZE = 3

    with patch("radis.pgsearch.tasks.app.configure_task") as cfg:
        count = enqueue_embed_reports([1, 2, 3, 4, 5, 6, 7])

    assert count == 3
    assert _defer_calls(cfg) == [
        {"report_ids": [1, 2, 3]},
        {"report_ids": [4, 5, 6]},
        {"report_ids": [7]},
    ]


def test_enqueue_embed_reports_helper_empty_input_is_noop():
    with patch("radis.pgsearch.tasks.app.configure_task") as cfg:
        count = enqueue_embed_reports([])
    assert count == 0
    cfg.assert_not_called()


def test_enqueue_embed_reports_helper_explicit_subjob_size_overrides_setting(settings):
    """Operators (e.g., `embed_pending --subjob-size=…`) can pass a
    one-off override without mutating the global setting."""
    settings.EMBEDDING_SUBJOB_SIZE = 100

    with patch("radis.pgsearch.tasks.app.configure_task") as cfg:
        count = enqueue_embed_reports([1, 2, 3, 4, 5], subjob_size=2)

    assert count == 3
    assert _defer_calls(cfg) == [
        {"report_ids": [1, 2]},
        {"report_ids": [3, 4]},
        {"report_ids": [5]},
    ]


def test_enqueue_embed_reports_defaults_to_live_priority(settings):
    """Write-path enqueues (no explicit priority) use LIVE so they preempt
    any backfill subjobs already sitting in the embeddings queue."""
    settings.EMBEDDING_LIVE_PRIORITY = 7
    settings.EMBEDDING_BACKFILL_PRIORITY = 0

    with patch("radis.pgsearch.tasks.app.configure_task") as cfg:
        enqueue_embed_reports([1])

    cfg.assert_called_once_with(
        "radis.pgsearch.tasks.embed_reports_task",
        allow_unknown=False,
        priority=7,
    )


def test_enqueue_embed_reports_explicit_backfill_priority(settings):
    """`embed_pending` and the admin backfill action pass
    BACKFILL_PRIORITY so they don't starve subsequent live writes."""
    settings.EMBEDDING_LIVE_PRIORITY = 7
    settings.EMBEDDING_BACKFILL_PRIORITY = 0

    with patch("radis.pgsearch.tasks.app.configure_task") as cfg:
        enqueue_embed_reports([1], priority=settings.EMBEDDING_BACKFILL_PRIORITY)

    cfg.assert_called_once_with(
        "radis.pgsearch.tasks.embed_reports_task",
        allow_unknown=False,
        priority=0,
    )


def test_timeout_propagates_after_retries(settings, no_retry_sleep):
    """A chunk that surfaces `openai.APITimeoutError` past the transient
    retry budget propagates so Procrastinate retries the whole subjob:
    1 initial call + EMBEDDING_TRANSIENT_RETRY_ATTEMPTS retries."""
    settings.EMBEDDING_BATCH_SIZE = 2
    reports = [ReportFactory.create() for _ in range(2)]
    pks = [r.pk for r in reports]

    fake = MagicMock()
    fake.__enter__ = MagicMock(return_value=fake)
    fake.__exit__ = MagicMock(return_value=None)
    fake.embed_documents = MagicMock(side_effect=_timeout_error())

    with patch("radis.pgsearch.tasks.EmbeddingClient", return_value=fake):
        with pytest.raises(openai.APITimeoutError):
            embed_reports_task(report_ids=pks)

    assert fake.embed_documents.call_count == 1 + settings.EMBEDDING_TRANSIENT_RETRY_ATTEMPTS
    assert ReportSearchIndex.objects.filter(embedding__isnull=True).count() == 2


def test_transient_retries_then_succeeds(settings, no_retry_sleep):
    """Transient EmbeddingClientError is retried locally: an embed call that
    fails the first two attempts and succeeds on the third returns vectors
    without escalating to Procrastinate's task-level retry."""
    settings.EMBEDDING_BATCH_SIZE = 4
    reports = [ReportFactory.create() for _ in range(3)]
    pks = [r.pk for r in reports]
    vec = _unit_vec(settings.EMBEDDING_DIM)

    fake = MagicMock()
    fake.__enter__ = MagicMock(return_value=fake)
    fake.__exit__ = MagicMock(return_value=None)
    fake.embed_documents = MagicMock(
        side_effect=[
            EmbeddingClientError("blip 1"),
            EmbeddingClientError("blip 2"),
            [vec, vec, vec],
        ]
    )

    with patch("radis.pgsearch.tasks.EmbeddingClient", return_value=fake):
        embed_reports_task(report_ids=pks)

    # The mock was called 3 times: two retries + one success.
    assert fake.embed_documents.call_count == 3
    # All three reports got embeddings; none stayed NULL.
    assert ReportSearchIndex.objects.filter(embedding__isnull=True).count() == 0


def test_logs_error_on_client_failure_and_reraises(caplog_tasks):
    reports = [ReportFactory.create() for _ in range(2)]
    pks = [r.pk for r in reports]
    fake = MagicMock()
    fake.__enter__ = MagicMock(return_value=fake)
    fake.__exit__ = MagicMock(return_value=None)
    fake.embed_documents = MagicMock(side_effect=EmbeddingClientError("service down"))

    with patch("radis.pgsearch.tasks.EmbeddingClient", return_value=fake):
        with pytest.raises(EmbeddingClientError):
            embed_reports_task(report_ids=pks)

    error_msgs = [r.getMessage() for r in caplog_tasks.records if r.levelname == "ERROR"]
    assert any(
        "embed_reports_task: embedding client failure after retries" in m for m in error_msgs
    )


def test_enqueue_embed_reports_logs_info_with_counts_and_priority(settings, caplog_tasks):
    settings.EMBEDDING_SUBJOB_SIZE = 3
    with patch("radis.pgsearch.tasks.app.configure_task"):
        enqueue_embed_reports([1, 2, 3, 4, 5, 6, 7], priority=5)

    info_msgs = [r.getMessage() for r in caplog_tasks.records if r.levelname == "INFO"]
    assert any(
        "enqueue_embed_reports: deferred 3 subjob(s) for 7 report(s) at priority=5" in m
        for m in info_msgs
    )


def test_logs_info_finish_with_counts_and_duration(settings, caplog_tasks):
    reports = [ReportFactory.create() for _ in range(2)]
    pks = [r.pk for r in reports]
    vec = _unit_vec(settings.EMBEDDING_DIM)
    fake = _make_fake_client(vec)

    with patch("radis.pgsearch.tasks.EmbeddingClient", return_value=fake):
        embed_reports_task(report_ids=pks)

    info_msgs = [r.getMessage() for r in caplog_tasks.records if r.levelname == "INFO"]
    finish = [m for m in info_msgs if "embed_reports_task: finished" in m]
    assert finish, info_msgs
    assert "embedded=2" in finish[0]
    assert "duration_ms=" in finish[0]


def test_transient_retry_emits_warning_log(settings, no_retry_sleep, caplog):
    """A retried transient blip must surface as a WARNING from the shared
    retry helper so operators see flakiness that never escalates to a
    task failure."""
    settings.EMBEDDING_BATCH_SIZE = 4
    reports = [ReportFactory.create() for _ in range(2)]
    pks = [r.pk for r in reports]
    vec = _unit_vec(settings.EMBEDDING_DIM)

    fake = MagicMock()
    fake.__enter__ = MagicMock(return_value=fake)
    fake.__exit__ = MagicMock(return_value=None)
    fake.embed_documents = MagicMock(side_effect=[EmbeddingClientError("blip"), [vec, vec]])

    rate_limit_logger = logging.getLogger("radis.core.utils.rate_limit")
    rate_limit_logger.addHandler(caplog.handler)
    try:
        with caplog.at_level(logging.WARNING, logger="radis.core.utils.rate_limit"):
            with patch("radis.pgsearch.tasks.EmbeddingClient", return_value=fake):
                embed_reports_task(report_ids=pks)
    finally:
        rate_limit_logger.removeHandler(caplog.handler)

    warning_msgs = [r.getMessage() for r in caplog.records if r.levelname == "WARNING"]
    assert any("Transient error on attempt 1" in m for m in warning_msgs)


@pytest.mark.django_db(False)
def test_embed_chunk_retries_connection_error_then_succeeds(no_retry_sleep):
    """Transient SDK errors are retried locally inside the chunk call."""
    from radis.pgsearch import tasks as tasks_module

    fake_client = MagicMock()
    fake_client.embed_documents = MagicMock(
        side_effect=[openai.APIConnectionError(request=None), [[0.1, 0.2]]]  # type: ignore[arg-type]
    )

    assert tasks_module._embed_chunk_with_retry(fake_client, ["hello"]) == [[0.1, 0.2]]
    assert fake_client.embed_documents.call_count == 2


@pytest.mark.django_db(False)
def test_embed_chunk_does_not_retry_rate_limit_error_locally():
    """429 must reach the rate-limit gate untouched, not be retried locally
    with a wait that ignores the server's own hint. (The gate is patched to
    a passthrough here, so the 429 surfaces directly.)"""
    from radis.pgsearch import tasks as tasks_module

    response = httpx.Response(429, request=httpx.Request("POST", "http://x"))
    fake_client = MagicMock()
    fake_client.embed_documents = MagicMock(
        side_effect=openai.RateLimitError(message="slow", response=response, body=None)
    )

    with pytest.raises(openai.RateLimitError):
        tasks_module._embed_chunk_with_retry(fake_client, ["hello"])
    assert fake_client.embed_documents.call_count == 1


@pytest.mark.django_db(False)
def test_embed_chunk_does_not_retry_rate_limited():
    """RateLimited (gate budget exhausted) must escape the transient retry
    layer so the Procrastinate retry strategy reschedules the whole subjob."""
    from radis.core.utils.rate_limit import RateLimited
    from radis.pgsearch import tasks as tasks_module

    fake_client = MagicMock()
    fake_client.embed_documents = MagicMock(side_effect=RateLimited())

    with pytest.raises(RateLimited):
        tasks_module._embed_chunk_with_retry(fake_client, ["hello"])
    assert fake_client.embed_documents.call_count == 1


@pytest.mark.django_db(False)
def test_embed_reports_task_retries_transient_errors_via_procrastinate():
    """The task must carry the Procrastinate retry strategy so RateLimited
    and transient errors reschedule the subjob instead of failing it
    permanently on first escape."""
    from radis.core.utils.rate_limit import RateLimited
    from radis.pgsearch.tasks import EMBEDDING_TASK_RETRY_STRATEGY, embed_reports_task

    assert embed_reports_task.retry_strategy is EMBEDDING_TASK_RETRY_STRATEGY
    retry_exceptions = EMBEDDING_TASK_RETRY_STRATEGY.retry_exceptions or set()
    assert RateLimited in retry_exceptions
    assert EmbeddingClientError in retry_exceptions
    # APIConnectionError covers APITimeoutError (subclass); Procrastinate
    # matches via isinstance.
    assert openai.APIConnectionError in retry_exceptions
    assert openai.InternalServerError in retry_exceptions


def test_cancel_backfill_embeddings_cancels_only_queued_backfill_jobs(settings):
    from procrastinate.contrib.django.models import ProcrastinateJob

    from radis.pgsearch import tasks as tasks_module

    tasks_module.enqueue_embed_reports(
        [1, 2, 3], subjob_size=1, priority=settings.EMBEDDING_BACKFILL_PRIORITY
    )
    tasks_module.enqueue_embed_reports([4], priority=settings.EMBEDDING_LIVE_PRIORITY)

    cancelled = tasks_module.cancel_backfill_embeddings()

    assert cancelled == 3
    by_priority = {
        priority: status
        for priority, status in ProcrastinateJob.objects.filter(
            task_name="radis.pgsearch.tasks.embed_reports_task"
        ).values_list("priority", "status")
    }
    assert by_priority[settings.EMBEDDING_BACKFILL_PRIORITY] == "cancelled"
    assert by_priority[settings.EMBEDDING_LIVE_PRIORITY] == "todo"


def test_cancel_backfill_embeddings_returns_zero_when_queue_empty():
    from radis.pgsearch import tasks as tasks_module

    assert tasks_module.cancel_backfill_embeddings() == 0


def test_embed_cancel_command_reports_count(settings):
    from io import StringIO

    from django.core.management import call_command

    from radis.pgsearch import tasks as tasks_module

    tasks_module.enqueue_embed_reports(
        [1, 2], subjob_size=1, priority=settings.EMBEDDING_BACKFILL_PRIORITY
    )

    out = StringIO()
    call_command("embed_cancel", stdout=out)

    assert "Cancelled 2 queued backfill subjob(s)" in out.getvalue()


def test_embed_cancel_command_handles_empty_queue():
    from io import StringIO

    from django.core.management import call_command

    out = StringIO()
    call_command("embed_cancel", stdout=out)

    assert "No queued backfill subjobs to cancel." in out.getvalue()


def test_embed_reports_task_increments_run_counter_and_flips_finished():
    reports = [ReportFactory.create() for _ in range(3)]
    run = EmbeddingBackfillRun.objects.create(total_reports=3, triggered_by="test")
    with _mock_embedding_client():
        embed_reports_task([r.pk for r in reports], run_id=run.pk)
    run.refresh_from_db()
    assert run.processed_reports == 3
    assert run.finished_at is not None


def test_embed_reports_task_partial_progress_leaves_run_unfinished():
    reports = [ReportFactory.create() for _ in range(2)]
    run = EmbeddingBackfillRun.objects.create(total_reports=5, triggered_by="test")
    with _mock_embedding_client():
        embed_reports_task([r.pk for r in reports], run_id=run.pk)
    run.refresh_from_db()
    assert run.processed_reports == 2
    assert run.finished_at is None


def test_embed_reports_task_failure_does_not_increment_counter():
    reports = [ReportFactory.create() for _ in range(2)]
    run = EmbeddingBackfillRun.objects.create(total_reports=2, triggered_by="test")
    with _mock_embedding_client(error=EmbeddingClientError("boom")):
        with pytest.raises(EmbeddingClientError):
            embed_reports_task([r.pk for r in reports], run_id=run.pk)
    run.refresh_from_db()
    assert run.processed_reports == 0
    assert run.finished_at is None


def test_embed_reports_task_never_finishes_cancelled_run():
    reports = [ReportFactory.create() for _ in range(2)]
    run = EmbeddingBackfillRun.objects.create(
        total_reports=2, triggered_by="test", cancelled_at=timezone.now()
    )
    with _mock_embedding_client():
        embed_reports_task([r.pk for r in reports], run_id=run.pk)
    run.refresh_from_db()
    assert run.processed_reports == 2  # counter stays truthful
    assert run.finished_at is None  # but a cancelled run never "finishes"


def test_embed_reports_task_without_run_id_touches_no_run():
    reports = [ReportFactory.create() for _ in range(1)]
    run = EmbeddingBackfillRun.objects.create(total_reports=9, triggered_by="test")
    with _mock_embedding_client():
        embed_reports_task([r.pk for r in reports])
    run.refresh_from_db()
    assert run.processed_reports == 0
