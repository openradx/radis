"""Tests for the `embed_pending` management command."""
from io import StringIO
from unittest.mock import AsyncMock, MagicMock, patch

import numpy as np
import pytest
from django.core.management import call_command

from radis.pgsearch.models import ReportSearchVector
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


def test_nothing_to_embed():
    out = StringIO()
    with patch(
        "radis.pgsearch.utils.inline_embedding.AsyncEmbeddingClient"
    ) as mock_client:
        call_command("embed_pending", stdout=out)
    assert "Nothing to embed." in out.getvalue()
    mock_client.assert_not_called()


def test_embeds_all_pending_in_batches(settings):
    # ReportFactory triggers the FTS post_save signal → RSV row with embedding=NULL.
    [ReportFactory.create() for _ in range(5)]
    assert ReportSearchVector.objects.filter(embedding__isnull=True).count() == 5
    vec = _unit_vec(settings.EMBEDDING_DIM)
    fake = _make_fake_async_client(vec)

    out = StringIO()
    with patch(
        "radis.pgsearch.utils.inline_embedding.AsyncEmbeddingClient",
        return_value=fake,
    ):
        call_command("embed_pending", "--batch-size", "2", stdout=out)

    assert ReportSearchVector.objects.filter(embedding__isnull=True).count() == 0
    output = out.getvalue()
    # 5 reports / batch 2 → progress at 2/5, 4/5, 5/5
    assert "2/5" in output
    assert "5/5" in output
    assert "Done." in output


def test_limit_caps_work(settings):
    [ReportFactory.create() for _ in range(5)]
    vec = _unit_vec(settings.EMBEDDING_DIM)
    fake = _make_fake_async_client(vec)

    out = StringIO()
    with patch(
        "radis.pgsearch.utils.inline_embedding.AsyncEmbeddingClient",
        return_value=fake,
    ):
        call_command("embed_pending", "--limit", "3", "--batch-size", "10", stdout=out)

    assert ReportSearchVector.objects.filter(embedding__isnull=True).count() == 2


def test_service_failure_does_not_crash_and_leaves_rows_null(settings):
    """If AsyncEmbeddingClient raises, embed_reports_inline catches it and the
    command keeps going. Reports stay NULL so the next run retries them."""
    from radis.pgsearch.utils.embedding_client import EmbeddingClientError

    [ReportFactory.create() for _ in range(3)]
    fake = MagicMock()
    fake.__aenter__ = AsyncMock(return_value=fake)
    fake.__aexit__ = AsyncMock(return_value=None)
    fake.embed_documents = AsyncMock(side_effect=EmbeddingClientError("service down"))

    out = StringIO()
    with patch(
        "radis.pgsearch.utils.inline_embedding.AsyncEmbeddingClient",
        return_value=fake,
    ):
        # Must not raise.
        call_command("embed_pending", stdout=out)

    # Rows still NULL — next run picks them up.
    assert ReportSearchVector.objects.filter(embedding__isnull=True).count() == 3
    assert "Done." in out.getvalue()
