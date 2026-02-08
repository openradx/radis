from __future__ import annotations

import logging
from itertools import batched

from django.conf import settings
from procrastinate.contrib.django import app

from radis.reports.models import Report

from .models import LabelGroup
from .processors import LabelGroupProcessor

logger = logging.getLogger(__name__)


@app.task(queue="llm")
def process_label_group(
    label_group_id: int, report_ids: list[int], overwrite_existing: bool = False
) -> None:
    group = LabelGroup.objects.get(id=label_group_id)
    processor = LabelGroupProcessor(group)
    processor.process_reports(report_ids, overwrite_existing=overwrite_existing)


def enqueue_labeling_for_reports(
    report_ids: list[int],
    groups: list[LabelGroup] | None = None,
    overwrite_existing: bool = False,
) -> None:
    if not report_ids:
        return

    if groups is None:
        active_groups = list(LabelGroup.objects.filter(is_active=True))
    else:
        active_groups = [group for group in groups if group.is_active]
    if not active_groups:
        logger.info("No active label groups found, skipping labeling for reports %s", report_ids)
        return

    batch_size = settings.LABELING_TASK_BATCH_SIZE
    for group in active_groups:
        for report_batch in batched(report_ids, batch_size):
            process_label_group.defer(
                label_group_id=group.id,
                report_ids=list(report_batch),
                overwrite_existing=overwrite_existing,
            )


@app.task
def enqueue_label_group_backfill(label_group_id: int) -> None:
    group = LabelGroup.objects.get(id=label_group_id)
    if not group.is_active:
        logger.info("Label group %s is inactive. Skipping backfill.", group)
        return

    batch_size = settings.LABELING_TASK_BATCH_SIZE
    current_batch: list[int] = []
    report_ids = Report.objects.order_by("id").values_list("id", flat=True).iterator(
        chunk_size=batch_size
    )

    for report_id in report_ids:
        current_batch.append(report_id)
        if len(current_batch) >= batch_size:
            process_label_group.defer(
                label_group_id=group.id,
                report_ids=current_batch,
                overwrite_existing=False,
            )
            current_batch = []

    if current_batch:
        process_label_group.defer(
            label_group_id=group.id,
            report_ids=current_batch,
            overwrite_existing=False,
        )
