from django.conf import settings
from procrastinate.contrib.django import app
from procrastinate.types import JSONValue

from radis.reports.models import Report
from radis.reports.site import (
    ReportsCreatedHandler,
    ReportsUpdatedHandler,
    register_reports_created_handler,
    register_reports_updated_handler,
    reports_created_handlers,
    reports_updated_handlers,
)

HANDLER_NAME = "labels"


def _chunked(items: list[JSONValue], size: int):
    for i in range(0, len(items), size):
        yield items[i : i + size]


def _label_reports_handler(reports: list[Report]) -> None:
    if not reports:
        return
    deferrer = app.configure_task(
        "radis.labels.tasks.label_report_batch",
        allow_unknown=False,
        priority=settings.LABELING_INGEST_PRIORITY,
    )
    report_ids: list[JSONValue] = [int(r.pk) for r in reports]
    for chunk in _chunked(report_ids, settings.LABELING_TASK_BATCH_SIZE):
        deferrer.defer(report_ids=chunk)


def register_report_handlers() -> None:
    if not any(h.name == HANDLER_NAME for h in reports_created_handlers):
        register_reports_created_handler(
            ReportsCreatedHandler(name=HANDLER_NAME, handle=_label_reports_handler)
        )
    if not any(h.name == HANDLER_NAME for h in reports_updated_handlers):
        register_reports_updated_handler(
            ReportsUpdatedHandler(name=HANDLER_NAME, handle=_label_reports_handler)
        )
