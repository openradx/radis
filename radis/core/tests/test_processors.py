from unittest.mock import patch

import pytest
import time_machine
from adit_radis_shared.accounts.factories import UserFactory
from django.utils import timezone

from radis.core.models import AnalysisJob, AnalysisTask
from radis.core.processors import AnalysisTaskProcessor
from radis.extractions.factories import ExtractionJobFactory, ExtractionTaskFactory


@pytest.mark.django_db
def test_processor_initialization():
    user = UserFactory.create()
    job = ExtractionJobFactory.create(owner=user)
    task = ExtractionTaskFactory.create(job=job)

    processor = AnalysisTaskProcessor(task)

    assert processor.task == task


@pytest.mark.django_db
def test_start_with_canceled_job_status_canceling():
    user = UserFactory.create()
    job = ExtractionJobFactory.create(owner=user, status=AnalysisJob.Status.CANCELING)
    task = ExtractionTaskFactory.create(job=job, status=AnalysisTask.Status.PENDING)

    processor = AnalysisTaskProcessor(task)

    with patch.object(job, "update_job_state") as mock_update_job_state:
        processor.start()

    task.refresh_from_db()
    assert task.status == AnalysisTask.Status.CANCELED
    assert task.started_at is not None
    assert task.ended_at is not None
    mock_update_job_state.assert_called_once()


@pytest.mark.django_db
def test_start_with_canceled_job_status_canceled():
    user = UserFactory.create()
    job = ExtractionJobFactory.create(owner=user, status=AnalysisJob.Status.CANCELED)
    task = ExtractionTaskFactory.create(job=job, status=AnalysisTask.Status.PENDING)

    processor = AnalysisTaskProcessor(task)

    with patch.object(job, "update_job_state") as mock_update_job_state:
        processor.start()

    task.refresh_from_db()
    assert task.status == AnalysisTask.Status.CANCELED
    assert task.started_at is not None
    assert task.ended_at is not None
    mock_update_job_state.assert_called_once()


@pytest.mark.django_db
def test_start_with_canceled_task_status():
    user = UserFactory.create()
    job = ExtractionJobFactory.create(owner=user, status=AnalysisJob.Status.PENDING)
    task = ExtractionTaskFactory.create(job=job, status=AnalysisTask.Status.CANCELED)

    processor = AnalysisTaskProcessor(task)

    with patch.object(job, "update_job_state") as mock_update_job_state:
        processor.start()

    task.refresh_from_db()
    assert task.status == AnalysisTask.Status.CANCELED
    assert task.started_at is not None
    assert task.ended_at is not None
    mock_update_job_state.assert_called_once()


@pytest.mark.django_db
@time_machine.travel("2025-01-15 14:30:00+00:00")
def test_start_job_transition_from_pending_to_in_progress():
    user = UserFactory.create()
    job = ExtractionJobFactory.create(owner=user, status=AnalysisJob.Status.PENDING)
    task = ExtractionTaskFactory.create(job=job, status=AnalysisTask.Status.PENDING)

    processor = AnalysisTaskProcessor(task)

    with (
        patch.object(processor, "process_task") as mock_process_task,
        patch.object(job, "update_job_state") as mock_update_job_state,
    ):
        processor.start()

    job.refresh_from_db()
    task.refresh_from_db()

    expected_time = timezone.now()
    assert job.status == AnalysisJob.Status.IN_PROGRESS
    assert job.started_at is not None
    assert abs((job.started_at - expected_time).total_seconds()) < 1
    assert task.status == AnalysisTask.Status.SUCCESS
    assert task.started_at is not None
    assert task.ended_at is not None
    mock_process_task.assert_called_once_with(task)
    mock_update_job_state.assert_called_once()


@pytest.mark.django_db
def test_start_job_already_in_progress():
    user = UserFactory.create()
    job = ExtractionJobFactory.create(
        owner=user, status=AnalysisJob.Status.IN_PROGRESS, started_at=timezone.now()
    )
    task = ExtractionTaskFactory.create(job=job, status=AnalysisTask.Status.PENDING)
    original_started_at = job.started_at

    processor = AnalysisTaskProcessor(task)

    with (
        patch.object(processor, "process_task") as mock_process_task,
        patch.object(job, "update_job_state") as mock_update_job_state,
    ):
        processor.start()

    job.refresh_from_db()
    task.refresh_from_db()

    assert job.status == AnalysisJob.Status.IN_PROGRESS
    assert job.started_at == original_started_at
    assert task.status == AnalysisTask.Status.SUCCESS
    mock_process_task.assert_called_once_with(task)
    mock_update_job_state.assert_called_once()


@pytest.mark.django_db
def test_start_task_processing_custom_status():
    user = UserFactory.create()
    job = ExtractionJobFactory.create(owner=user, status=AnalysisJob.Status.IN_PROGRESS)
    task = ExtractionTaskFactory.create(job=job, status=AnalysisTask.Status.PENDING)

    processor = AnalysisTaskProcessor(task)

    def mock_process_task_with_warning(task):
        task.status = AnalysisTask.Status.WARNING
        task.save()

    with (
        patch.object(processor, "process_task", side_effect=mock_process_task_with_warning),
        patch.object(job, "update_job_state") as mock_update_job_state,
    ):
        processor.start()

    task.refresh_from_db()

    assert task.status == AnalysisTask.Status.WARNING
    assert task.started_at is not None
    assert task.ended_at is not None
    mock_update_job_state.assert_called_once()


@pytest.mark.django_db
def test_start_task_processing_exception():
    user = UserFactory.create()
    job = ExtractionJobFactory.create(owner=user, status=AnalysisJob.Status.IN_PROGRESS)
    task = ExtractionTaskFactory.create(job=job, status=AnalysisTask.Status.PENDING)

    processor = AnalysisTaskProcessor(task)
    error_message = "Test processing error"

    def mock_process_task_with_exception(task):
        raise ValueError(error_message)

    with (
        patch.object(processor, "process_task", side_effect=mock_process_task_with_exception),
        patch.object(job, "update_job_state") as mock_update_job_state,
        patch("radis.core.processors.logger") as mock_logger,
    ):
        processor.start()

    task.refresh_from_db()

    assert task.status == AnalysisTask.Status.FAILURE
    assert task.message == error_message
    assert task.started_at is not None
    assert task.ended_at is not None
    assert "ValueError: Test processing error" in task.log
    assert "Traceback" in task.log
    mock_logger.exception.assert_called_once_with("Task %s failed.", task)
    mock_update_job_state.assert_called_once()


@pytest.mark.django_db
def test_start_task_processing_exception_with_existing_log():
    user = UserFactory.create()
    job = ExtractionJobFactory.create(owner=user, status=AnalysisJob.Status.IN_PROGRESS)
    task = ExtractionTaskFactory.create(
        job=job, status=AnalysisTask.Status.PENDING, log="Previous log entry"
    )

    processor = AnalysisTaskProcessor(task)
    error_message = "Test processing error"

    def mock_process_task_with_exception(task):
        raise ValueError(error_message)

    with (
        patch.object(processor, "process_task", side_effect=mock_process_task_with_exception),
        patch.object(job, "update_job_state") as mock_update_job_state,
    ):
        processor.start()

    task.refresh_from_db()

    assert task.status == AnalysisTask.Status.FAILURE
    assert task.message == error_message
    assert "Previous log entry" in task.log
    assert "\n---\n" in task.log
    assert "ValueError: Test processing error" in task.log
    mock_update_job_state.assert_called_once()


@pytest.mark.django_db
def test_start_logging_info_messages():
    user = UserFactory.create()
    job = ExtractionJobFactory.create(owner=user, status=AnalysisJob.Status.IN_PROGRESS)
    task = ExtractionTaskFactory.create(job=job, status=AnalysisTask.Status.PENDING)

    processor = AnalysisTaskProcessor(task)

    with (
        patch.object(processor, "process_task"),
        patch.object(job, "update_job_state"),
        patch("radis.core.processors.logger") as mock_logger,
    ):
        processor.start()

    mock_logger.info.assert_any_call("Start processing task %s", task)
    mock_logger.info.assert_any_call("Task %s ended", task)


@pytest.mark.django_db
def test_process_task_default_implementation():
    user = UserFactory.create()
    job = ExtractionJobFactory.create(owner=user)
    task = ExtractionTaskFactory.create(job=job)

    processor = AnalysisTaskProcessor(task)

    # Should not raise any exception
    result = processor.process_task(task)
    assert result is None


@pytest.mark.django_db
def test_start_assertion_error_on_invalid_task_status():
    user = UserFactory.create()
    job = ExtractionJobFactory.create(owner=user, status=AnalysisJob.Status.PENDING)
    task = ExtractionTaskFactory.create(job=job, status=AnalysisTask.Status.SUCCESS)

    processor = AnalysisTaskProcessor(task)

    with pytest.raises(AssertionError):
        processor.start()


@pytest.mark.django_db
def test_start_assertion_error_on_invalid_job_status():
    user = UserFactory.create()
    job = ExtractionJobFactory.create(owner=user, status=AnalysisJob.Status.SUCCESS)
    task = ExtractionTaskFactory.create(job=job, status=AnalysisTask.Status.PENDING)

    processor = AnalysisTaskProcessor(task)

    with pytest.raises(AssertionError):
        processor.start()
