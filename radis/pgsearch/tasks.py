import logging
from typing import Any

from procrastinate.contrib.django import app

from .utils.indexing import bulk_upsert_report_search_vectors

logger = logging.getLogger(__name__)


@app.task
def bulk_index_reports(report_ids: list[int]) -> None:
    if not report_ids:
        return
    logger.info("Indexing %s reports in bulk.", len(report_ids))
    bulk_upsert_report_search_vectors(report_ids)


def enqueue_bulk_index_reports(report_ids: list[int]) -> int | None:
    if not report_ids:
        return None
    payload: list[Any] = [int(report_id) for report_id in report_ids]
    return app.configure_task(
        "radis.pgsearch.tasks.bulk_index_reports",
        allow_unknown=False,
    ).defer(report_ids=payload)
