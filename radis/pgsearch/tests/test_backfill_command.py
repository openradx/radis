from io import StringIO
from unittest.mock import patch

import pytest
from django.conf import settings
from django.core.management import call_command

from radis.pgsearch.models import ReportSearchVector
from radis.reports.factories import ReportFactory


@pytest.mark.django_db
def test_backfill_enqueues_only_null_embeddings():
    r_null = ReportFactory.create()
    r_filled = ReportFactory.create()
    ReportSearchVector.objects.filter(report=r_filled).update(
        embedding=[1.0] + [0.0] * (settings.EMBEDDING_DIM - 1)
    )

    with patch(
        "radis.pgsearch.management.commands.backfill_embeddings.enqueue_embed_reports"
    ) as enqueue:
        call_command("backfill_embeddings", batch_size=10, stdout=StringIO())

    # Only the null-embedding report should be in any of the enqueue calls.
    enqueued_ids = [rid for call in enqueue.call_args_list for rid in call.args[0]]
    assert r_null.pk in enqueued_ids
    assert r_filled.pk not in enqueued_ids


@pytest.mark.django_db
def test_backfill_chunks_by_batch_size():
    [ReportFactory.create() for _ in range(5)]

    with patch(
        "radis.pgsearch.management.commands.backfill_embeddings.enqueue_embed_reports"
    ) as enqueue:
        call_command("backfill_embeddings", batch_size=2, stdout=StringIO())

    sizes = [len(call.args[0]) for call in enqueue.call_args_list]
    assert sizes == [2, 2, 1]


@pytest.mark.django_db
def test_backfill_limit_caps_total():
    [ReportFactory.create() for _ in range(5)]

    with patch(
        "radis.pgsearch.management.commands.backfill_embeddings.enqueue_embed_reports"
    ) as enqueue:
        call_command("backfill_embeddings", batch_size=10, limit=3, stdout=StringIO())

    enqueued_ids = [rid for call in enqueue.call_args_list for rid in call.args[0]]
    assert len(enqueued_ids) == 3


@pytest.mark.django_db
def test_backfill_dry_run_does_not_enqueue():
    [ReportFactory.create() for _ in range(3)]
    out = StringIO()

    with patch(
        "radis.pgsearch.management.commands.backfill_embeddings.enqueue_embed_reports"
    ) as enqueue:
        call_command("backfill_embeddings", dry_run=True, stdout=out)

    enqueue.assert_not_called()
    assert "would enqueue 3" in out.getvalue().lower()


@pytest.mark.django_db
def test_backfill_uses_backfill_priority():
    ReportFactory.create()
    with patch(
        "radis.pgsearch.management.commands.backfill_embeddings.enqueue_embed_reports"
    ) as enqueue:
        call_command("backfill_embeddings", stdout=StringIO())
    assert enqueue.call_args.kwargs["priority"] == settings.EMBEDDING_BACKFILL_PRIORITY


@pytest.mark.django_db
def test_backfill_rejects_zero_batch_size():
    from django.core.management.base import CommandError

    with pytest.raises(CommandError, match="--batch-size must be > 0"):
        call_command("backfill_embeddings", batch_size=0, stdout=StringIO())


@pytest.mark.django_db
def test_backfill_rejects_negative_limit():
    from django.core.management.base import CommandError

    with pytest.raises(CommandError, match="--limit must be >= 0"):
        call_command("backfill_embeddings", limit=-1, stdout=StringIO())
