from unittest.mock import patch

import pytest

from radis.reports.factories import ReportFactory
from radis.reports.site import (
    reports_created_handlers,
    reports_updated_handlers,
)
from radis.labels.signals import (
    HANDLER_NAME,
    _label_reports_handler,
    register_report_handlers,
)


class TestRegistration:
    def test_registers_created_and_updated(self):
        register_report_handlers()
        assert any(h.name == HANDLER_NAME for h in reports_created_handlers)
        assert any(h.name == HANDLER_NAME for h in reports_updated_handlers)

    def test_idempotent(self):
        register_report_handlers()
        before_c = sum(1 for h in reports_created_handlers if h.name == HANDLER_NAME)
        before_u = sum(1 for h in reports_updated_handlers if h.name == HANDLER_NAME)
        register_report_handlers()
        assert sum(1 for h in reports_created_handlers if h.name == HANDLER_NAME) == before_c
        assert sum(1 for h in reports_updated_handlers if h.name == HANDLER_NAME) == before_u


class TestHandlerChunking:
    @pytest.mark.parametrize(
        "n, expected_chunks",
        [(1, 1), (100, 1), (101, 2), (250, 3), (0, 0)],
    )
    def test_chunks_correctly(self, n, expected_chunks, settings):
        settings.LABELING_TASK_BATCH_SIZE = 100
        reports = [ReportFactory() for _ in range(n)]
        with patch("radis.labels.signals.app") as app_mock:
            deferrer = app_mock.configure_task.return_value
            _label_reports_handler(reports)
        assert deferrer.defer.call_count == expected_chunks

    def test_uses_ingest_priority(self, settings):
        settings.LABELING_INGEST_PRIORITY = 7
        with patch("radis.labels.signals.app") as app_mock:
            _label_reports_handler([ReportFactory()])
        _, kw = app_mock.configure_task.call_args
        assert kw["priority"] == 7

    def test_preserves_report_ids(self):
        reports = [ReportFactory() for _ in range(5)]
        ids = [r.id for r in reports]
        with patch("radis.labels.signals.app") as app_mock:
            deferrer = app_mock.configure_task.return_value
            _label_reports_handler(reports)
        deferred_ids = [c.kwargs["report_ids"] for c in deferrer.defer.call_args_list]
        assert sorted(sum(deferred_ids, [])) == sorted(ids)
