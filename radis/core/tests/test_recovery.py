from datetime import timedelta
from unittest.mock import patch

import pytest
from adit_radis_shared.accounts.factories import UserFactory
from django.db import connection
from django.utils import timezone
from procrastinate.contrib.django.models import ProcrastinateJob, ProcrastinateWorker

from radis.core.models import AnalysisJob, AnalysisTask
from radis.core.utils import recovery
from radis.core.utils.recovery import sweep_stale_analysis_state
from radis.extractions.factories import ExtractionJobFactory, ExtractionTaskFactory
from radis.extractions.models import ExtractionTask


@pytest.fixture(autouse=True)
def writable_procrastinate(settings):
    settings.PROCRASTINATE_READONLY_MODELS = False


def create_worker(heartbeat_age_seconds: int) -> ProcrastinateWorker:
    return ProcrastinateWorker.objects.create(
        last_heartbeat=timezone.now() - timedelta(seconds=heartbeat_age_seconds)
    )


def create_row(status: str, worker: ProcrastinateWorker | None = None) -> ProcrastinateJob:
    return ProcrastinateJob.objects.create(
        queue_name="llm",
        task_name="radis.extractions.tasks.process_extraction_task",
        priority=0,
        args={},
        status=status,
        attempts=0,
        abort_requested=False,
        worker=worker,
    )


def make_stale_task(job_status, row: ProcrastinateJob | None) -> ExtractionTask:
    user = UserFactory.create()
    job = ExtractionJobFactory.create(owner=user, status=job_status)
    return ExtractionTaskFactory.create(
        job=job, status=AnalysisTask.Status.IN_PROGRESS, queued_job=row
    )


def owner_gone_q():
    cutoff = timezone.now() - timedelta(seconds=30)
    return recovery._owner_gone_q(cutoff)


@pytest.mark.django_db
def test_orphan_under_canceling_job_is_canceled():
    # The reported bug: task IN_PROGRESS, queue row gone, job CANCELING.
    task = make_stale_task(AnalysisJob.Status.CANCELING, row=None)

    sweep_stale_analysis_state()

    task.refresh_from_db()
    task.job.refresh_from_db()
    assert task.status == AnalysisTask.Status.CANCELED
    assert task.message == "The worker processing this task was terminated."
    assert task.ended_at is not None
    assert task.job.status == AnalysisJob.Status.CANCELED


@pytest.mark.django_db
def test_orphan_under_live_job_is_requeued():
    task = make_stale_task(AnalysisJob.Status.IN_PROGRESS, row=None)

    with patch.object(ExtractionTask, "delay", autospec=True) as mock_delay:
        sweep_stale_analysis_state()

    task.refresh_from_db()
    task.job.refresh_from_db()
    assert task.status == AnalysisTask.Status.PENDING
    assert task.ended_at is None
    mock_delay.assert_called_once()
    assert task.job.status == AnalysisJob.Status.PENDING  # not terminal


@pytest.mark.django_db
def test_todo_row_with_stale_worker_is_reset_without_requeue():
    row = create_row("todo", worker=create_worker(heartbeat_age_seconds=60))
    task = make_stale_task(AnalysisJob.Status.IN_PROGRESS, row=row)

    with patch.object(ExtractionTask, "delay", autospec=True) as mock_delay:
        sweep_stale_analysis_state()

    task.refresh_from_db()
    assert task.status == AnalysisTask.Status.PENDING
    mock_delay.assert_not_called()  # retry_stalled_jobs re-queued that same row


@pytest.mark.django_db
def test_doing_row_with_stale_worker_is_reset_without_requeue():
    row = create_row("doing", worker=create_worker(heartbeat_age_seconds=60))
    task = make_stale_task(AnalysisJob.Status.IN_PROGRESS, row=row)

    with patch.object(ExtractionTask, "delay", autospec=True) as mock_delay:
        sweep_stale_analysis_state()

    task.refresh_from_db()
    assert task.status == AnalysisTask.Status.PENDING
    mock_delay.assert_not_called()
    # Procrastinate's own row is never touched.
    assert ProcrastinateJob.objects.get(pk=row.pk).status == "doing"


@pytest.mark.django_db
def test_doing_row_with_fresh_worker_is_left_alone():
    # The long-LLM-batch guarantee.
    row = create_row("doing", worker=create_worker(heartbeat_age_seconds=0))
    task = make_stale_task(AnalysisJob.Status.IN_PROGRESS, row=row)

    with patch.object(ExtractionTask, "delay", autospec=True) as mock_delay:
        sweep_stale_analysis_state()

    task.refresh_from_db()
    assert task.status == AnalysisTask.Status.IN_PROGRESS
    assert task.queued_job_id == row.pk
    mock_delay.assert_not_called()


@pytest.mark.django_db
def test_doing_row_without_worker_is_treated_as_dead():
    row = create_row("doing", worker=None)
    task = make_stale_task(AnalysisJob.Status.IN_PROGRESS, row=row)

    with patch.object(ExtractionTask, "delay", autospec=True) as mock_delay:
        sweep_stale_analysis_state()

    task.refresh_from_db()
    assert task.status == AnalysisTask.Status.PENDING
    mock_delay.assert_not_called()  # the doing row still fires via retry_stalled_jobs


@pytest.mark.django_db
@pytest.mark.parametrize("row_status", ["succeeded", "failed"])
def test_terminal_row_is_resolved_like_an_orphan(row_status):
    row = create_row(row_status)
    task = make_stale_task(AnalysisJob.Status.IN_PROGRESS, row=row)

    with patch.object(ExtractionTask, "delay", autospec=True) as mock_delay:
        sweep_stale_analysis_state()

    task.refresh_from_db()
    assert task.status == AnalysisTask.Status.PENDING
    mock_delay.assert_called_once()


@pytest.mark.django_db
@pytest.mark.parametrize("task_status", [AnalysisTask.Status.PENDING, AnalysisTask.Status.SUCCESS])
def test_non_in_progress_tasks_are_never_touched(task_status):
    user = UserFactory.create()
    job = ExtractionJobFactory.create(owner=user, status=AnalysisJob.Status.IN_PROGRESS)
    task = ExtractionTaskFactory.create(job=job, status=task_status, queued_job=None)

    with patch.object(ExtractionTask, "delay", autospec=True) as mock_delay:
        sweep_stale_analysis_state()

    task.refresh_from_db()
    assert task.status == task_status
    mock_delay.assert_not_called()


@pytest.mark.django_db
def test_resolve_twice_changes_nothing_the_second_time():
    task = make_stale_task(AnalysisJob.Status.IN_PROGRESS, row=None)
    stale_copy = ExtractionTask.objects.get(pk=task.pk)  # second sweep's stale candidate

    with patch.object(ExtractionTask, "delay", autospec=True) as mock_delay:
        assert recovery._resolve_stale_task(task, owner_gone_q()) == "pending"
        assert recovery._resolve_stale_task(stale_copy, owner_gone_q()) is None

    assert mock_delay.call_count == 1


@pytest.mark.django_db
def test_requeue_decision_uses_fresh_read_not_snapshot():
    # The row exists (todo) at candidate-selection time, but fires and is deleted before
    # the resolve step runs. The snapshot says "row exists, don't re-queue" — the fresh
    # read must notice the row is gone and call delay(), else the task is lost forever.
    row = create_row("todo", worker=create_worker(heartbeat_age_seconds=60))
    task = make_stale_task(AnalysisJob.Status.IN_PROGRESS, row=row)
    task = ExtractionTask.objects.select_related("queued_job").get(pk=task.pk)  # snapshot

    with connection.cursor() as cursor:  # raw delete, like Procrastinate's --delete-jobs
        cursor.execute("DELETE FROM procrastinate_jobs WHERE id = %s", [row.pk])

    with patch.object(ExtractionTask, "delay", autospec=True) as mock_delay:
        assert recovery._resolve_stale_task(task, owner_gone_q()) == "pending"

    mock_delay.assert_called_once()


@pytest.mark.django_db
def test_resolve_declines_when_row_is_doing_under_fresh_worker():
    # Owner-gone is re-checked inside the conditional UPDATE: a candidate whose row a live
    # worker claimed in the meantime must not be flipped.
    row = create_row("doing", worker=create_worker(heartbeat_age_seconds=0))
    task = make_stale_task(AnalysisJob.Status.IN_PROGRESS, row=row)

    with patch.object(ExtractionTask, "delay", autospec=True) as mock_delay:
        assert recovery._resolve_stale_task(task, owner_gone_q()) is None

    task.refresh_from_db()
    assert task.status == AnalysisTask.Status.IN_PROGRESS
    mock_delay.assert_not_called()
