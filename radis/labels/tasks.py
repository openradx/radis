from __future__ import annotations

import logging
from datetime import datetime
from itertools import batched

from django.conf import settings
from django.db import transaction
from django.utils import timezone
from procrastinate.contrib.django import app

from radis.reports.models import Report

from .models import BackfillJob, LabelingRun, QuestionSet
from .processors import LabelingProcessor

logger = logging.getLogger(__name__)


_PROCESS_BATCH_TASK_NAME = "radis.labels.tasks.process_question_set_batch"


def _run_modes() -> list[str]:
    """Modes a labelling pass should produce. Defaults to DIRECT only.

    The reasoning commit flips this default to include REASONED so every
    report ends up with both modes available for evaluation.
    """
    return getattr(settings, "LABELS_RUN_MODES", [LabelingRun.Mode.DIRECT])


def _defer_batch(
    *, question_set_id: int, report_ids: list[int], mode: str, priority: int,
    backfill_job_id: int | None = None,
) -> None:
    """Defer a labelling batch onto the llm queue with an explicit priority.

    Centralized so live ingest (high priority) and backfill coordinators
    (low priority) can't accidentally drift apart.
    """
    kwargs: dict = {
        "question_set_id": question_set_id,
        "report_ids": report_ids,
        "mode": mode,
    }
    if backfill_job_id is not None:
        kwargs["backfill_job_id"] = backfill_job_id
    app.configure_task(
        _PROCESS_BATCH_TASK_NAME,
        allow_unknown=False,
        priority=priority,
    ).defer(**kwargs)


@app.task(queue="llm")
def process_question_set_batch(
    question_set_id: int,
    report_ids: list[int],
    mode: str,
    backfill_job_id: int | None = None,
) -> None:
    """Run one mode over a batch of reports. One LabelingRun per report.

    If part of a backfill, the worker first checks the backfill row's status
    so an in-flight cancel takes effect without needing to drain the queue.
    Cancellation is finalized synchronously by the cancel view, so a CANCELED
    job needs no further finalization here — the worker just bails out cleanly.
    """
    if backfill_job_id is not None:
        try:
            backfill_job = BackfillJob.objects.get(id=backfill_job_id)
        except BackfillJob.DoesNotExist:
            logger.warning("Backfill job %s not found, skipping batch.", backfill_job_id)
            return

        if backfill_job.status == BackfillJob.Status.CANCELED:
            logger.info("Backfill job %s is CANCELED, skipping batch.", backfill_job)
            return

    question_set = QuestionSet.objects.get(id=question_set_id)
    processor = LabelingProcessor(question_set, mode=mode)
    processor.process_reports(report_ids)

    if backfill_job_id is not None:
        _maybe_finalize(backfill_job_id)


def _maybe_finalize(backfill_job_id: int) -> None:
    """Finalize the backfill as SUCCESS if no work remains.

    Completeness is derived from the actual ``LabelingRun`` rows rather than
    from a hand-maintained counter, so this check is correct even when
    batches crash, retry, or when reports are deleted mid-backfill.

    Cancellation does not flow through this helper: the cancel view sets
    ``CANCELED`` synchronously. The conditional ``UPDATE`` here only matches
    ``IN_PROGRESS`` rows, so a concurrent cancel that already moved the job
    to ``CANCELED`` cannot be silently overwritten.
    """
    try:
        backfill_job = BackfillJob.objects.get(id=backfill_job_id)
    except BackfillJob.DoesNotExist:
        return

    if backfill_job.status != BackfillJob.Status.IN_PROGRESS:
        return

    if backfill_job.question_set.missing_reports().exists():
        return

    BackfillJob.objects.filter(
        id=backfill_job_id,
        status=BackfillJob.Status.IN_PROGRESS,
    ).update(
        status=BackfillJob.Status.SUCCESS,
        ended_at=timezone.now(),
        processed_reports=backfill_job.total_reports,
    )


def enqueue_labeling_for_reports(
    report_ids: list[int],
    question_sets: list[QuestionSet] | None = None,
) -> None:
    """Schedule labelling of a small set of reports across all active sets.

    Used by the per-report ingest path: newly-created or updated reports get
    labelled high-priority so dashboards reflect them as soon as the LLM can
    catch up. For each question set, one task is dispatched per mode in
    ``LABELS_RUN_MODES``.
    """
    if not report_ids:
        return

    if question_sets is None:
        active_sets = list(QuestionSet.objects.filter(is_active=True))
    else:
        active_sets = [qs for qs in question_sets if qs.is_active]
    if not active_sets:
        logger.info("No active question sets, skipping labeling for reports %s", report_ids)
        return

    batch_size = settings.LABELING_TASK_BATCH_SIZE
    for question_set in active_sets:
        for mode in _run_modes():
            for report_batch in batched(report_ids, batch_size):
                _defer_batch(
                    question_set_id=question_set.id,
                    report_ids=list(report_batch),
                    mode=mode,
                    priority=settings.LABELS_LIVE_PRIORITY,
                )


@app.periodic(cron=settings.LABELS_BACKFILL_CRON)
@app.task()
def labels_backfill_launcher(timestamp: int) -> None:
    """Nightly: scan every active question set for outstanding labelling
    work and dispatch one backfill per dirty set.

    "Dirty" means ``missing_reports()`` returns at least one row — this
    catches all cases (new questions added, new reports ingested without
    a labelling run, prior backfill that failed, question version bump)
    without enumerating them.

    The dispatch is wrapped in a per-set ``select_for_update`` so two
    overlapping launcher runs (e.g. if a previous tick is still finishing
    when the next one fires) cannot both create a backfill for the same
    set. The lock is also what makes the staff-edit guard in views.py
    safe: while a backfill is pending or in progress, edits are rejected
    and the launcher will not stack a second backfill on top.
    """
    logger.info("Labels backfill launcher tick (timestamp=%s)", datetime.fromtimestamp(timestamp))

    for question_set in QuestionSet.objects.filter(is_active=True):
        if not question_set.missing_reports().exists():
            continue

        with transaction.atomic():
            QuestionSet.objects.select_for_update().filter(id=question_set.id).first()

            already_active = BackfillJob.objects.filter(
                question_set=question_set,
                status__in=[BackfillJob.Status.PENDING, BackfillJob.Status.IN_PROGRESS],
            ).exists()
            if already_active:
                logger.info(
                    "Skipping launcher dispatch for set %s — backfill already active.",
                    question_set,
                )
                continue

            backfill_job = BackfillJob.objects.create(question_set=question_set)

            def _defer(jid=backfill_job.id, sid=question_set.id):
                enqueue_question_set_backfill.defer(
                    question_set_id=sid, backfill_job_id=jid
                )

            transaction.on_commit(_defer)

            logger.info(
                "Dispatched nightly backfill for set %s (job=%s).",
                question_set,
                backfill_job.id,
            )


@app.task
def enqueue_question_set_backfill(question_set_id: int, backfill_job_id: int) -> None:
    """Backfill coordinator. Dispatches one batch task per (batch, mode).

    Runs on the default queue (no LLM call here). Each task it enqueues runs
    on the ``llm`` queue.
    """
    question_set = QuestionSet.objects.get(id=question_set_id)
    if not question_set.is_active:
        logger.info("Question set %s is inactive. Skipping backfill.", question_set)
        BackfillJob.objects.filter(id=backfill_job_id).update(
            status=BackfillJob.Status.CANCELED,
            message="Question set is inactive.",
            ended_at=timezone.now(),
        )
        return

    try:
        backfill_job = BackfillJob.objects.get(id=backfill_job_id)
    except BackfillJob.DoesNotExist:
        logger.warning("Backfill job %s not found, aborting.", backfill_job_id)
        return

    total_reports = Report.objects.count()
    backfill_job.status = BackfillJob.Status.IN_PROGRESS
    backfill_job.started_at = timezone.now()
    backfill_job.ended_at = None
    backfill_job.message = ""
    backfill_job.total_reports = total_reports
    backfill_job.processed_reports = 0
    backfill_job.save()

    if total_reports == 0:
        backfill_job.status = BackfillJob.Status.SUCCESS
        backfill_job.message = "No reports to process."
        backfill_job.ended_at = timezone.now()
        backfill_job.save()
        return

    # Dispatch missing reports only; this makes the backfill naturally
    # resumable since a retry skips everything that's already complete.
    batch_size = settings.LABELING_TASK_BATCH_SIZE
    modes = _run_modes()

    dispatched_any = False
    current_batch: list[int] = []
    missing_iter = (
        question_set.missing_reports()
        .order_by("id")
        .values_list("id", flat=True)
        .iterator(chunk_size=batch_size)
    )

    def dispatch_batch(batch: list[int]) -> None:
        nonlocal dispatched_any
        for mode in modes:
            _defer_batch(
                question_set_id=question_set.id,
                report_ids=batch,
                mode=mode,
                priority=settings.LABELS_BACKFILL_PRIORITY,
                backfill_job_id=backfill_job.id,
            )
        dispatched_any = True

    for report_id in missing_iter:
        current_batch.append(report_id)
        if len(current_batch) >= batch_size:
            dispatch_batch(current_batch)
            current_batch = []

    if current_batch:
        dispatch_batch(current_batch)

    if not dispatched_any:
        backfill_job.status = BackfillJob.Status.SUCCESS
        backfill_job.message = "All reports already labelled."
        backfill_job.ended_at = timezone.now()
        backfill_job.processed_reports = total_reports
        backfill_job.save()
