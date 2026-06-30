"""Tests for `embed_reports_task` and its chaining from `bulk_index_reports`."""

import logging
from unittest.mock import MagicMock, patch

import numpy as np
import pytest
import stamina

from radis.pgsearch.models import ReportSearchIndex
from radis.pgsearch.tasks import (
    bulk_index_reports,
    embed_reports_task,
    enqueue_embed_reports,
)
from radis.pgsearch.utils.embedding_client import (
    EmbeddingClientError,
    EmbeddingPayloadTooLargeError,
)
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
    root handler doesn't see records emitted under it. Yield caplog
    so tests can assert on `caplog.records`."""
    task_logger = logging.getLogger("radis.pgsearch.tasks")
    task_logger.addHandler(caplog.handler)
    caplog.set_level(logging.DEBUG, logger="radis.pgsearch.tasks")
    try:
        yield caplog
    finally:
        task_logger.removeHandler(caplog.handler)


pytestmark = pytest.mark.django_db(transaction=True)


def _unit_vec(dim: int) -> list[float]:
    v = np.ones(dim, dtype=np.float32)
    return (v / np.linalg.norm(v)).tolist()


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


def test_bisects_on_too_large_and_isolates_offender(settings, caplog, monkeypatch):
    """When the backend rejects a batch as too large, the task bisects until
    it isolates the single offending report, logs ERROR with its id + body
    length, skips it, and still embeds the rest of the batch."""
    settings.EMBEDDING_BATCH_SIZE = 4
    reports = [ReportFactory.create() for _ in range(4)]
    pks = [r.pk for r in reports]
    offender_pk = pks[2]  # the third report is the one we mark too large

    vec = _unit_vec(settings.EMBEDDING_DIM)

    def fake_embed(texts):
        # Simulate the backend rejecting any payload that contains the
        # offending report's body. The body is fetched by report_id.
        offender_body = (
            ReportSearchIndex.objects.select_related("report")
            .get(report_id=offender_pk)
            .report.body
        )
        if offender_body in texts:
            raise EmbeddingPayloadTooLargeError("over context window")
        return [vec] * len(texts)

    fake = MagicMock()
    fake.__enter__ = MagicMock(return_value=fake)
    fake.__exit__ = MagicMock(return_value=None)
    fake.embed_documents = MagicMock(side_effect=fake_embed)

    # The project's `radis` logger has `propagate=False` in settings, so
    # caplog's root handler doesn't see records emitted under it. Attach
    # caplog's handler directly to the task logger for the duration of
    # this test.
    task_logger = logging.getLogger("radis.pgsearch.tasks")
    task_logger.addHandler(caplog.handler)
    caplog.set_level(logging.ERROR, logger="radis.pgsearch.tasks")
    try:
        with patch("radis.pgsearch.tasks.EmbeddingClient", return_value=fake):
            embed_reports_task(report_ids=pks)
    finally:
        task_logger.removeHandler(caplog.handler)

    # The three good reports got embeddings; the offender stayed NULL.
    rsvs_by_pk = {
        rsv.report.pk: rsv
        for rsv in ReportSearchIndex.objects.filter(report_id__in=pks).select_related("report")
    }
    assert rsvs_by_pk[offender_pk].embedding is None
    for pk in pks:
        if pk == offender_pk:
            continue
        assert rsvs_by_pk[pk].embedding is not None

    # The bisect logged the specific offender's id + body length, and the
    # task-level summary listed it among skipped ids.
    error_msgs = [r.getMessage() for r in caplog.records if r.levelname == "ERROR"]
    assert any(f"report_id={offender_pk}" in msg and "body_chars=" in msg for msg in error_msgs)
    assert any("skipped as too large" in msg and str(offender_pk) in msg for msg in error_msgs)


def test_non_too_large_error_propagates_without_bisecting():
    """A generic EmbeddingClientError (5xx, network, etc.) must NOT bisect —
    Procrastinate's retry policy should handle it, retrying the whole batch.
    (Stamina retries are disabled in the conftest, so this is a single call.)"""
    reports = [ReportFactory.create() for _ in range(4)]
    pks = [r.pk for r in reports]
    fake = MagicMock()
    fake.__enter__ = MagicMock(return_value=fake)
    fake.__exit__ = MagicMock(return_value=None)
    fake.embed_documents = MagicMock(side_effect=EmbeddingClientError("service down"))

    with patch("radis.pgsearch.tasks.EmbeddingClient", return_value=fake):
        with pytest.raises(EmbeddingClientError):
            embed_reports_task(report_ids=pks)

    # Only one call should have been made — no bisect on non-too-large errors.
    assert fake.embed_documents.call_count == 1
    assert ReportSearchIndex.objects.filter(embedding__isnull=True).count() == 4


def test_stamina_retries_transient_then_succeeds(settings, stamina_active):
    """stamina retries transient EmbeddingClientError: an embed call that
    fails the first two attempts and succeeds on the third returns vectors
    without the bisect logic ever firing, and without escalating to
    Procrastinate's task-level retry."""
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


def test_stamina_does_not_retry_payload_too_large(settings, stamina_active):
    """EmbeddingPayloadTooLargeError must skip the stamina retry layer and
    go straight to the bisect logic. With one offender in a single-row
    chunk, the embed_documents mock should be called exactly once (no
    retries), and the offender is logged + skipped."""
    settings.EMBEDDING_BATCH_SIZE = 1
    reports = [ReportFactory.create() for _ in range(1)]
    pks = [r.pk for r in reports]

    fake = MagicMock()
    fake.__enter__ = MagicMock(return_value=fake)
    fake.__exit__ = MagicMock(return_value=None)
    fake.embed_documents = MagicMock(side_effect=EmbeddingPayloadTooLargeError("over context"))

    with patch("radis.pgsearch.tasks.EmbeddingClient", return_value=fake):
        embed_reports_task(report_ids=pks)

    # Single call — no stamina retry for payload-too-large.
    assert fake.embed_documents.call_count == 1
    assert ReportSearchIndex.objects.filter(embedding__isnull=True).count() == 1
