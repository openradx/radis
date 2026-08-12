import logging
from datetime import datetime, timedelta

from django.apps import apps
from django.conf import settings
from django.db.models import Q
from django.utils import timezone
from procrastinate.contrib.django.models import ProcrastinateJob
from procrastinate.jobs import Status

from radis.core.models import AnalysisJob, AnalysisTask

logger = logging.getLogger(__name__)

# Row statuses under which no worker is executing the row. ABORTING is documented as
# legacy and unused; a row in any status other than "doing" is not being run by anyone.
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
    """The worker that was running the task is gone.

    Positive disjunctions on purpose: with a nullable join, an .exclude() of the
    live case evaluates to NULL for orphans and silently drops them.
    """
    return (
        # queue row deleted (Procrastinate runs with --delete-jobs=always)
        Q(queued_job__isnull=True)
        # queue row is not being run by anyone: already re-queued, or finished
        | Q(queued_job__status__in=_INACTIVE_ROW_STATUSES)
        # queue row says doing, but its worker row was pruned
        | Q(queued_job__status=Status.DOING.value, queued_job__worker__isnull=True)
        # queue row says doing, but its worker stopped sending heartbeats
        | Q(queued_job__status=Status.DOING.value, queued_job__worker__last_heartbeat__lt=cutoff)
    )


def _resolve_stale_task(task: AnalysisTask, owner_gone: Q) -> str | None:
    """Repair one stale candidate. Returns the outcome, or None if the race was lost."""
    model = type(task)
    job = task.job

    if job.status in (AnalysisJob.Status.CANCELING, AnalysisJob.Status.CANCELED):
        new_status = AnalysisTask.Status.CANCELED
        ended_at = timezone.now()
    else:
        new_status = AnalysisTask.Status.PENDING
        ended_at = None

    stale_job_id = task.queued_job_id  # capture before the update nulls it

    # Conditional UPDATE re-checks status AND owner-gone at execution time, closing the
    # common orderings of concurrent sweeps and live-worker claims. A residual sub-ms EPQ
    # window can still yield a duplicate queue row; the processor's PENDING claim discards it.
    updated = (
        model.objects.filter(pk=task.pk, status=AnalysisTask.Status.IN_PROGRESS)
        .filter(owner_gone)
        .update(
            status=new_status,
            message="The worker processing this task was terminated.",
            ended_at=ended_at,
            queued_job_id=None,
        )
    )
    if not updated:
        return None  # another sweep won, or a live worker claimed the task meanwhile

    if new_status == AnalysisTask.Status.PENDING:
        # Fresh read — never the candidate snapshot: the row can fire, fail its claim and
        # be deleted mid-sweep, and a PENDING task with no row is invisible to every
        # future sweep. Re-queue only when no live row remains.
        row_alive = (
            stale_job_id is not None
            and ProcrastinateJob.objects.filter(
                pk=stale_job_id, status__in=_LIVE_ROW_STATUSES
            ).exists()
        )
        if not row_alive:
            task.refresh_from_db()  # delay() does a full save; don't write back stale fields
            task.delay()

    return "pending" if new_status == AnalysisTask.Status.PENDING else "canceled"


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
        if job.status not in _TERMINAL_JOB_STATUSES:
            job.update_job_state()

    # One summary line: the sweep runs at container boot, and a large recovery must not
    # bury the worker's startup output.
    logger.info("Swept stale analysis state: %s", ", ".join(summary))
