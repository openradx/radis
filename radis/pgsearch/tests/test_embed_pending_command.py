"""Tests for the `embed_pending` management command."""
from io import StringIO
from unittest.mock import patch

import pytest
from django.conf import settings
from django.core.management import call_command

from radis.reports.factories import ReportFactory

pytestmark = pytest.mark.django_db


def test_nothing_to_embed():
    out = StringIO()
    with patch(
        "radis.pgsearch.management.commands.embed_pending.enqueue_embed_reports"
    ) as enqueue:
        call_command("embed_pending", stdout=out)
    assert "Nothing to embed." in out.getvalue()
    enqueue.assert_not_called()


def test_enqueues_via_helper_with_explicit_subjob_size():
    # ReportFactory triggers the FTS post_save signal → RSV row with embedding=NULL.
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
    assert kwargs["priority"] == settings.EMBEDDING_BACKFILL_PRIORITY

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
        call_command(
            "embed_pending", "--limit", "3", "--subjob-size", "10", stdout=out
        )

    args, kwargs = enqueue.call_args
    assert len(args[0]) == 3
    assert kwargs["subjob_size"] == 10
    assert kwargs["priority"] == settings.EMBEDDING_BACKFILL_PRIORITY
