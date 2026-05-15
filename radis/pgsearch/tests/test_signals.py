from unittest.mock import patch

import pytest

from radis.reports.factories import ReportFactory


@pytest.mark.django_db
def test_report_save_enqueues_embed_reports():
    with patch("radis.pgsearch.signals.enqueue_embed_reports") as enqueue:
        report = ReportFactory.create()
    # factory_boy calls instance.save() twice (initial create + after post-generation
    # hooks), so enqueue_embed_reports is called at least once with the report PK.
    assert enqueue.call_count >= 1
    enqueue.assert_called_with([report.pk])


@pytest.mark.django_db
def test_report_update_also_enqueues_embed_reports():
    report = ReportFactory.create()
    with patch("radis.pgsearch.signals.enqueue_embed_reports") as enqueue:
        report.body = "Updated body"
        report.save()
    enqueue.assert_called_once_with([report.pk])
