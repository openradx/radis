"""Tests for `embed_reports_task` and its chaining from `bulk_index_reports`."""
import logging
from unittest.mock import MagicMock, patch

import numpy as np
import pytest
import stamina

from radis.pgsearch.models import ReportSearchVector
from radis.pgsearch.tasks import bulk_index_reports, embed_reports_task
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
    assert ReportSearchVector.objects.filter(embedding__isnull=True).count() == 0


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

    assert ReportSearchVector.objects.filter(embedding__isnull=True).count() == 2


def test_bulk_index_reports_chains_into_embed_reports_task():
    """`bulk_index_reports` upserts RSVs and then defers `embed_reports_task`.
    The chain is the ordering guarantee: the embeddings worker only ever sees
    report ids whose RSV rows are already committed."""
    reports = [ReportFactory.create() for _ in range(3)]
    pks = [r.pk for r in reports]
    ReportSearchVector.objects.filter(report_id__in=pks).delete()

    with patch("radis.pgsearch.tasks.embed_reports_task.defer") as defer:
        bulk_index_reports(report_ids=pks)

    # RSVs were upserted, then the embed task was deferred with the same ids.
    assert ReportSearchVector.objects.filter(report_id__in=pks).count() == 3
    defer.assert_called_once_with(report_ids=pks)


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
        offender_body = ReportSearchVector.objects.select_related("report").get(
            report_id=offender_pk
        ).report.body
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
        rsv.report_id: rsv
        for rsv in ReportSearchVector.objects.filter(report_id__in=pks)
    }
    assert rsvs_by_pk[offender_pk].embedding is None
    for pk in pks:
        if pk == offender_pk:
            continue
        assert rsvs_by_pk[pk].embedding is not None

    # The bisect logged the specific offender's id + body length, and the
    # task-level summary listed it among skipped ids.
    error_msgs = [r.getMessage() for r in caplog.records if r.levelname == "ERROR"]
    assert any(
        f"report_id={offender_pk}" in msg and "body_chars=" in msg
        for msg in error_msgs
    )
    assert any(
        "skipped as too large" in msg and str(offender_pk) in msg
        for msg in error_msgs
    )


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
    assert ReportSearchVector.objects.filter(embedding__isnull=True).count() == 4


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
    assert ReportSearchVector.objects.filter(embedding__isnull=True).count() == 0


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
    fake.embed_documents = MagicMock(
        side_effect=EmbeddingPayloadTooLargeError("over context")
    )

    with patch("radis.pgsearch.tasks.EmbeddingClient", return_value=fake):
        embed_reports_task(report_ids=pks)

    # Single call — no stamina retry for payload-too-large.
    assert fake.embed_documents.call_count == 1
    assert ReportSearchVector.objects.filter(embedding__isnull=True).count() == 1
