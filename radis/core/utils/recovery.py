import logging
from datetime import datetime, timedelta

from django.apps import apps
from django.conf import settings
from django.db import transaction
from django.db.models import Q
from django.utils import timezone
from procrastinate.contrib.django.models import ProcrastinateJob
from procrastinate.jobs import Status

from radis.core.models import AnalysisJob, AnalysisTask

logger = logging.getLogger(__name__)

# Queue row statuses that mean no worker is currently running the row (all but "doing").
_INACTIVE_ROW_STATUSES = [
    Status.TODO.value,
    Status.SUCCEEDED.value,
    Status.FAILED.value,
    Status.CANCELLED.value,
    Status.ABORTED.value,
]

_LIVE_ROW_STATUSES = [Status.TODO.value, Status.DOING.value]

_TERMINAL_JOB_STATUSES = (
    AnalysisJob.Status.CANCELED,
    AnalysisJob.Status.SUCCESS,
    AnalysisJob.Status.WARNING,
    AnalysisJob.Status.FAILURE,
)


def _analysis_task_models() -> list[type[AnalysisTask]]:
    return [m for m in apps.get_models() if issubclass(m, AnalysisTask)]


def _owner_gone_q(cutoff: datetime) -> Q:
    """Match tasks whose worker is gone (queue row missing, finished, or worker silent).

    Written as OR-ed positive conditions: an .exclude() would miss tasks that have no
    queue row at all (NULL join).
    """
    return (
        # queue row deleted (Procrastinate runs with --delete-jobs=always)
        Q(queued_job__isnull=True)
        # queue row is not being run by anyone: already re-queued, or finished
        | Q(queued_job__status__in=_INACTIVE_ROW_STATUSES)
        # queue row says doing, but its worker row no longer exists
        | Q(queued_job__status=Status.DOING.value, queued_job__worker__isnull=True)
        # queue row says doing, but its worker stopped sending heartbeats
        | Q(queued_job__status=Status.DOING.value, queued_job__worker__last_heartbeat__lt=cutoff)
    )


def _resolve_stale_task(task: AnalysisTask, owner_gone: Q) -> str | None:
    """Reset one stale task. Returns "pending"/"canceled", or None if it was no longer stale."""
    model = type(task)
    job = task.job

    if job.status in (AnalysisJob.Status.CANCELING, AnalysisJob.Status.CANCELED):
        new_status = AnalysisTask.Status.CANCELED
        ended_at = timezone.now()
    else:
        new_status = AnalysisTask.Status.PENDING
        ended_at = None

    old_row_id = task.queued_job_id

    # Reset and re-queue in one transaction: if delay() fails, the reset rolls back and the
    # next sweep retries. A PENDING task without a queue row would never run again.
    with transaction.atomic():
        # A PENDING task whose old row will fire again keeps pointing at that row. If the link
        # were dropped, the next sweep would see the re-run task as ownerless (NULL link) and
        # enqueue a second row for it while the first is still running.
        keep_link = new_status == AnalysisTask.Status.PENDING and _row_will_refire(old_row_id)

        # Re-check status and owner-gone inside the UPDATE itself, so nothing happens if
        # another sweep or a live worker got to this task first. (A rare duplicate queue
        # row is still possible; the processor's PENDING claim drops it.)
        changes = {
            "status": new_status,
            "message": "The worker processing this task was terminated.",
            "ended_at": ended_at,
        }
        if not keep_link:
            changes["queued_job_id"] = None
        updated = (
            model.objects.filter(pk=task.pk, status=AnalysisTask.Status.IN_PROGRESS)
            .filter(owner_gone)
            .update(**changes)
        )
        if not updated:
            return None  # another sweep or a live worker got here first

        # Re-queue only if no old row will fire again. Read the DB fresh once more: the row may
        # have fired, failed its claim and been deleted between the check above and the UPDATE.
        if new_status == AnalysisTask.Status.PENDING and not _row_will_refire(old_row_id):
            task.refresh_from_db()  # delay() saves the whole task; reload it first
            task.delay()

    return "pending" if new_status == AnalysisTask.Status.PENDING else "canceled"


def _row_will_refire(row_id: int | None) -> bool:
    """True if the queue row still exists and is todo/doing, so a worker will run it again."""
    return (
        row_id is not None
        and ProcrastinateJob.objects.filter(pk=row_id, status__in=_LIVE_ROW_STATUSES).exists()
    )


def sweep_stale_analysis_state() -> None:
    """Repair tasks left IN_PROGRESS by a killed worker, across all AnalysisTask models."""
    cutoff = timezone.now() - timedelta(seconds=settings.ANALYSIS_STALLED_WORKER_GRACE_SECONDS)
    owner_gone = _owner_gone_q(cutoff)

    summary: list[str] = []
    affected_jobs: dict[tuple[str, int], AnalysisJob] = {}

    for model in _analysis_task_models():
        pending = canceled = 0
        candidates = (
            model.objects.filter(status=AnalysisTask.Status.IN_PROGRESS)
            .filter(owner_gone)
            .select_related("job", "queued_job", "queued_job__worker")
        )
        for task in candidates:
            outcome = _resolve_stale_task(task, owner_gone)
            if outcome == "pending":
                pending += 1
            elif outcome == "canceled":
                canceled += 1
            else:
                continue
            affected_jobs[(task.job._meta.label, task.job.pk)] = task.job

        total = pending + canceled
        if total:
            summary.append(f"{model.__name__} {total} ({pending} pending, {canceled} canceled)")
        else:
            summary.append(f"{model.__name__} 0")

    for job in affected_jobs.values():
        job.refresh_from_db()
        # Re-evaluate the job unless it is already finished. A finished job is re-evaluated
        # too if it still has open tasks (should not happen, but a repaired task must never
        # be left hanging under a finished job).
        if (
            job.status not in _TERMINAL_JOB_STATUSES
            or job.tasks.filter(
                status__in=(AnalysisTask.Status.PENDING, AnalysisTask.Status.IN_PROGRESS)
            ).exists()
        ):
            job.update_job_state()

    # One summary line; INFO only if something was repaired (the sweep runs every minute).
    log = logger.info if affected_jobs else logger.debug
    log("Swept stale analysis state: %s", ", ".join(summary))
