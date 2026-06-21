"""Tests for `embed_reports_task`."""
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import numpy as np
import pytest

from radis.pgsearch.models import ReportSearchVector
from radis.pgsearch.tasks import embed_reports_task
from radis.pgsearch.utils.embedding_client import EmbeddingClientError
from radis.reports.factories import ReportFactory

pytestmark = pytest.mark.django_db(transaction=True)


def _unit_vec(dim: int) -> list[float]:
    v = np.ones(dim, dtype=np.float32)
    return (v / np.linalg.norm(v)).tolist()


def _make_fake_async_client(vec: list[float]) -> MagicMock:
    """Build a MagicMock that mimics `async with AsyncEmbeddingClient() as c`
    and supports `await c.embed_documents([...])`."""
    instance = MagicMock()
    instance.__aenter__ = AsyncMock(return_value=instance)
    instance.__aexit__ = AsyncMock(return_value=None)
    instance.embed_documents = AsyncMock(side_effect=lambda texts: [vec] * len(texts))
    return instance


def test_empty_input_no_ops():
    with patch("radis.pgsearch.tasks.AsyncEmbeddingClient") as client_cls:
        asyncio.run(embed_reports_task(report_ids=[]))
    client_cls.assert_not_called()


def test_no_matching_rsvs_no_ops():
    """Report ids that don't resolve to actual reports must not blow up;
    bulk_upsert_report_search_vectors logs+skips missing rows and the task
    returns without calling the embedding service."""
    with patch("radis.pgsearch.tasks.AsyncEmbeddingClient") as client_cls:
        asyncio.run(embed_reports_task(report_ids=[999_999]))
    client_cls.assert_not_called()


def test_embeds_in_internal_batches(settings):
    settings.EMBEDDING_BATCH_SIZE = 2
    reports = [ReportFactory.create() for _ in range(5)]
    pks = [r.pk for r in reports]
    vec = _unit_vec(settings.EMBEDDING_DIM)
    fake = _make_fake_async_client(vec)

    with patch("radis.pgsearch.tasks.AsyncEmbeddingClient", return_value=fake):
        asyncio.run(embed_reports_task(report_ids=pks))

    # 5 reports with batch_size=2 → 3 embed_documents calls of sizes 2, 2, 1.
    assert fake.embed_documents.await_count == 3
    sizes = [len(call.args[0]) for call in fake.embed_documents.await_args_list]
    assert sorted(sizes) == [1, 2, 2]
    assert ReportSearchVector.objects.filter(embedding__isnull=True).count() == 0


def test_embedding_error_propagates():
    """Procrastinate retries depend on the exception escaping the task."""
    reports = [ReportFactory.create() for _ in range(2)]
    pks = [r.pk for r in reports]
    fake = MagicMock()
    fake.__aenter__ = AsyncMock(return_value=fake)
    fake.__aexit__ = AsyncMock(return_value=None)
    fake.embed_documents = AsyncMock(side_effect=EmbeddingClientError("service down"))

    with patch("radis.pgsearch.tasks.AsyncEmbeddingClient", return_value=fake):
        with pytest.raises(EmbeddingClientError):
            asyncio.run(embed_reports_task(report_ids=pks))

    assert ReportSearchVector.objects.filter(embedding__isnull=True).count() == 2


def test_ensures_rsv_rows_exist_before_embedding(settings):
    """If a report has no ReportSearchVector yet (e.g., bulk_index_reports
    hasn't run, or an admin/shell edit bypassed the signal), the embed task
    must create the row + tsvector before reading body. This is the safety
    net that lets the handler enqueue embed without waiting for the
    deferred FTS task to land first."""
    reports = [ReportFactory.create() for _ in range(2)]
    pks = [r.pk for r in reports]
    ReportSearchVector.objects.filter(report_id__in=pks).delete()
    assert ReportSearchVector.objects.filter(report_id__in=pks).count() == 0

    vec = _unit_vec(settings.EMBEDDING_DIM)
    fake = _make_fake_async_client(vec)
    with patch("radis.pgsearch.tasks.AsyncEmbeddingClient", return_value=fake):
        asyncio.run(embed_reports_task(report_ids=pks))

    rsvs = ReportSearchVector.objects.filter(report_id__in=pks)
    assert rsvs.count() == 2
    assert rsvs.filter(search_vector__isnull=True).count() == 0
    assert rsvs.filter(embedding__isnull=True).count() == 0
