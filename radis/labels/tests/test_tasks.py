from unittest.mock import patch

from radis.core.models import AnalysisJob, AnalysisTask
from radis.labels.factories import (
    LabelingJobFactory,
    LabelingTaskFactory,
    QuestionFactory,
)
from radis.labels.tasks import (
    enqueue_all_pending_tasks,
    label_report_batch,
    process_labeling_job,
    process_labeling_task,
)
from radis.reports.factories import ReportFactory


def test_label_report_batch_calls_parallel_helper():
    with patch("radis.labels.tasks.label_reports_in_parallel") as helper:
        label_report_batch(report_ids=[1, 2, 3])
    helper.assert_called_once_with([1, 2, 3])


def test_process_labeling_task_invokes_processor():
    task = LabelingTaskFactory()
    with patch("radis.labels.tasks.LabelingTaskProcessor") as ProcessorMock:
        process_labeling_task(task_id=task.id)
    ProcessorMock.assert_called_once()
    ProcessorMock.return_value.start.assert_called_once()


class TestProcessLabelingJob:
    def test_prep_then_pending(self):
        job = LabelingJobFactory(status=AnalysisJob.Status.UNVERIFIED)
        QuestionFactory(active=True)
        for _ in range(2):
            ReportFactory()
        with patch("radis.labels.tasks.enqueue_all_pending_tasks"):
            process_labeling_job(job_id=job.id)
        job.refresh_from_db()
        assert job.status == AnalysisJob.Status.PENDING
        assert job.started_at is not None

    def test_deletes_pre_existing_tasks(self):
        job = LabelingJobFactory(status=AnalysisJob.Status.UNVERIFIED)
        # Simulate a crashed prior attempt: pre-existing tasks under this job.
        LabelingTaskFactory(job=job)
        LabelingTaskFactory(job=job)
        QuestionFactory(active=True)
        for _ in range(2):
            ReportFactory()
        with patch("radis.labels.tasks.enqueue_all_pending_tasks"):
            process_labeling_job(job_id=job.id)
        # Pre-existing tasks were deleted; new ones created for the 2 reports.
        # With default batch size 100 that's 1 new task.
        assert job.tasks.count() == 1


def test_enqueue_defers_one_procrastinate_job_per_pending_task():
    job = LabelingJobFactory(status=AnalysisJob.Status.PENDING)
    t1 = LabelingTaskFactory(job=job, status=AnalysisTask.Status.PENDING)
    t2 = LabelingTaskFactory(job=job, status=AnalysisTask.Status.PENDING)
    LabelingTaskFactory(job=job, status=AnalysisTask.Status.SUCCESS)  # skipped
    with patch("radis.labels.tasks.app") as app_mock:
        deferrer = app_mock.configure_task.return_value
        deferrer.defer.side_effect = [101, 102]
        enqueue_all_pending_tasks(job)
    assert deferrer.defer.call_count == 2
    deferred_ids = sorted(c.kwargs["task_id"] for c in deferrer.defer.call_args_list)
    assert deferred_ids == sorted([t1.id, t2.id])
