"""Tests for `embed_reports_task` and its chaining from `bulk_index_reports`."""

import logging
from unittest.mock import MagicMock, patch

import httpx
import numpy as np
import openai
import pytest
import stamina

from radis.pgsearch.models import ReportSearchIndex
from radis.pgsearch.tasks import (
    bulk_index_reports,
    embed_reports_task,
    enqueue_embed_reports,
)
from radis.pgsearch.utils.embedding_client import EmbeddingClientError
from radis.reports.factories import ReportFactory


@pytest.fixture
def stamina_active():
    """Enable stamina retries for the duration of one test. The repo-wide
    conftest disables them so the rest of the suite isn't slowed by retry
    backoffs."""
    stamina.set_active(True)
    yield
    stamina.set_active(False)


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
def _bypass_429_backoff(monkeypatch):
    """These tests exercise embed_reports_task's business logic (retry,
    logging), not the 429 backoff itself — that's covered in
    test_rate_limiter.py. Patch it to a passthrough so a stray 429 in a
    test double can't trigger real sleeps."""
    from radis.pgsearch import tasks as tasks_module

    monkeypatch.setattr(tasks_module, "call_with_429_backoff", lambda fn, **kwargs: fn())


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


def test_empty_input_no_ops():
    with patch("radis.pgsearch.tasks.EmbeddingClient") as client_cls:
        embed_reports_task(report_ids=[])
    client_cls.assert_not_called()


def test_no_matching_rsvs_no_ops():
    """Report ids that don't resolve to RSV rows are a no-op — the task does
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


def test_embed_chunk_with_retry_wraps_call_in_429_backoff(monkeypatch):
    from radis.pgsearch import tasks as tasks_module

    wrapped = {"called": False}

    def fake_call_with_429_backoff(fn, **kwargs):
        wrapped["called"] = True
        return fn()

    monkeypatch.setattr(tasks_module, "call_with_429_backoff", fake_call_with_429_backoff)

    fake_client = MagicMock()
    fake_client.embed_documents = MagicMock(return_value=[[0.1, 0.2]])

    result = tasks_module._embed_chunk_with_retry(fake_client, ["hello"])

    assert result == [[0.1, 0.2]]
    assert wrapped["called"] is True
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


def test_timeout_propagates_after_retries(settings):
    """A chunk that surfaces `openai.APITimeoutError` past stamina's retry
    budget propagates so Procrastinate retries the whole subjob. Stamina
    retries are disabled in the conftest, so this is a single call."""
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

    assert fake.embed_documents.call_count == 1
    assert ReportSearchIndex.objects.filter(embedding__isnull=True).count() == 2


def test_stamina_retries_transient_then_succeeds(settings, stamina_active):
    """stamina retries transient EmbeddingClientError: an embed call that
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


def test_truncate_ids_returns_first_n():
    from radis.pgsearch.tasks import _truncate_ids

    assert _truncate_ids([1, 2, 3], limit=50) == [1, 2, 3]
    assert _truncate_ids(list(range(100)), limit=3) == [0, 1, 2]
    assert _truncate_ids([], limit=10) == []


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


def test_log_stamina_retry_emits_warning_for_embed_call(caplog_tasks):
    from stamina.instrumentation import RetryDetails

    from radis.pgsearch.tasks import _log_stamina_retry

    details = RetryDetails(
        name="radis.pgsearch.tasks._embed_chunk_with_retry",
        args=(),
        kwargs={},
        retry_num=2,
        wait_for=1.25,
        waited_so_far=0.5,
        caused_by=RuntimeError("boom"),
    )
    _log_stamina_retry(details)

    warning_msgs = [r.getMessage() for r in caplog_tasks.records if r.levelname == "WARNING"]
    assert any(
        "embed_reports_task: embedding HTTP call failed; attempt=2 "
        "retrying in 1.25s. Error: boom" in m
        for m in warning_msgs
    )


def test_stamina_retry_emits_warning_through_registered_hook(
    settings, stamina_active, caplog_tasks
):
    settings.EMBEDDING_BATCH_SIZE = 4
    reports = [ReportFactory.create() for _ in range(2)]
    pks = [r.pk for r in reports]
    vec = _unit_vec(settings.EMBEDDING_DIM)

    fake = MagicMock()
    fake.__enter__ = MagicMock(return_value=fake)
    fake.__exit__ = MagicMock(return_value=None)
    fake.embed_documents = MagicMock(side_effect=[EmbeddingClientError("blip"), [vec, vec]])

    with patch("radis.pgsearch.tasks.EmbeddingClient", return_value=fake):
        embed_reports_task(report_ids=pks)

    warning_msgs = [r.getMessage() for r in caplog_tasks.records if r.levelname == "WARNING"]
    assert any("embedding HTTP call failed; attempt=1" in m for m in warning_msgs)


def test_log_stamina_retry_ignores_other_callsites(caplog_tasks):
    from stamina.instrumentation import RetryDetails

    from radis.pgsearch.tasks import _log_stamina_retry

    details = RetryDetails(
        name="some.other.module._other_retry",
        args=(),
        kwargs={},
        retry_num=1,
        wait_for=0.5,
        waited_so_far=0.0,
        caused_by=RuntimeError("not ours"),
    )
    _log_stamina_retry(details)

    warning_msgs = [r.getMessage() for r in caplog_tasks.records if r.levelname == "WARNING"]
    assert warning_msgs == []


@pytest.mark.django_db(False)
def test_predicate_retries_openai_connection_error():
    import openai

    from radis.pgsearch.tasks import _is_retryable_embedding_error

    exc = openai.APIConnectionError(request=None)  # type: ignore[arg-type]
    assert _is_retryable_embedding_error(exc) is True


@pytest.mark.django_db(False)
def test_predicate_retries_openai_internal_server_error():
    import httpx
    import openai

    from radis.pgsearch.tasks import _is_retryable_embedding_error

    # InternalServerError is an APIStatusError subclass; construct via the
    # SDK's __init__ which only requires message + response + body in modern
    # versions. Use a minimal httpx.Response to satisfy the signature.
    response = httpx.Response(503, request=httpx.Request("POST", "http://x"))
    exc = openai.InternalServerError(message="boom", response=response, body=None)
    assert _is_retryable_embedding_error(exc) is True


@pytest.mark.django_db(False)
def test_predicate_does_not_retry_openai_rate_limit_error():
    """429 must reach `call_with_429_backoff`, not be silently retried by
    stamina with a wait that ignores the server's own hint."""
    import httpx
    import openai

    from radis.pgsearch.tasks import _is_retryable_embedding_error

    response = httpx.Response(429, request=httpx.Request("POST", "http://x"))
    exc = openai.RateLimitError(message="slow", response=response, body=None)
    assert _is_retryable_embedding_error(exc) is False
