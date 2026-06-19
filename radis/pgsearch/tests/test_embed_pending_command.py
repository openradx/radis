"""Tests for the `embed_pending` management command."""
from io import StringIO
from unittest.mock import patch

import pytest
from django.core.management import call_command

from radis.reports.factories import ReportFactory

pytestmark = pytest.mark.django_db


def test_nothing_to_embed():
    out = StringIO()
    with patch("radis.pgsearch.management.commands.embed_pending.embed_reports_task") as task:
        call_command("embed_pending", stdout=out)
    assert "Nothing to embed." in out.getvalue()
    task.defer.assert_not_called()


def test_enqueues_all_pending_in_batches():
    # ReportFactory triggers the FTS post_save signal → RSV row with embedding=NULL.
    reports = [ReportFactory.create() for _ in range(5)]
    expected_ids = sorted(r.pk for r in reports)

    out = StringIO()
    with patch("radis.pgsearch.management.commands.embed_pending.embed_reports_task") as task:
        call_command("embed_pending", "--batch-size", "2", stdout=out)

    # 5 reports / batch 2 → three defer calls of sizes 2, 2, 1.
    assert task.defer.call_count == 3
    enqueued_ids = [pk for call in task.defer.call_args_list for pk in call.kwargs["report_ids"]]
    assert sorted(enqueued_ids) == expected_ids
    output = out.getvalue()
    assert "2/5" in output
    assert "5/5" in output
    assert "Done." in output


def test_limit_caps_work():
    [ReportFactory.create() for _ in range(5)]

    out = StringIO()
    with patch("radis.pgsearch.management.commands.embed_pending.embed_reports_task") as task:
        call_command("embed_pending", "--limit", "3", "--batch-size", "10", stdout=out)

    enqueued_ids = [pk for call in task.defer.call_args_list for pk in call.kwargs["report_ids"]]
    assert len(enqueued_ids) == 3
