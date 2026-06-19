"""Tests for `embed_reports_task` and the `bulk_index_reports` → embedding chain."""
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import numpy as np
import pytest

from radis.pgsearch.models import ReportSearchVector
from radis.pgsearch.tasks import bulk_index_reports, embed_reports_task
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


def test_bulk_index_reports_chains_into_embed_reports_task():
    """When PGSEARCH_SYNC_INDEXING=False, the deferred FTS task must enqueue
    the embedding task at the end so embedding always follows FTS."""
    reports = [ReportFactory.create() for _ in range(3)]
    pks = [r.pk for r in reports]

    with patch("radis.pgsearch.tasks.embed_reports_task") as task:
        bulk_index_reports(report_ids=pks)

    task.defer.assert_called_once()
    assert sorted(task.defer.call_args.kwargs["report_ids"]) == sorted(pks)
