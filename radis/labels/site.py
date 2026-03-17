from __future__ import annotations

from typing import Iterable

from django.db import transaction

from radis.reports.models import Report

from .tasks import enqueue_labeling_for_reports


def handle_reports_created(reports: Iterable[Report]) -> None:
    report_ids = [int(getattr(report, "id")) for report in reports]
    if not report_ids:
        return

    def on_commit() -> None:
        enqueue_labeling_for_reports(report_ids)

    transaction.on_commit(on_commit)


def handle_reports_updated(reports: Iterable[Report]) -> None:
    report_ids = [int(getattr(report, "id")) for report in reports]
    if not report_ids:
        return

    def on_commit() -> None:
        enqueue_labeling_for_reports(report_ids, overwrite_existing=True)

    transaction.on_commit(on_commit)
