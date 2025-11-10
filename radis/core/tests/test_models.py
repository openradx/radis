from unittest.mock import patch

import pytest
import time_machine
from adit_radis_shared.accounts.factories import UserFactory
from django.utils import timezone

from radis.core.models import AnalysisJob, AnalysisTask
from radis.extractions.factories import ExtractionJobFactory, ExtractionTaskFactory


class TestAnalysisJob:
    @pytest.mark.django_db
    def test_job_update_job_state_all_tasks_succeed(self):
        user = UserFactory.create()
        job = ExtractionJobFactory.create(owner=user, status=AnalysisJob.Status.PENDING)

        ExtractionTaskFactory.create(job=job, status=AnalysisTask.Status.SUCCESS)
        ExtractionTaskFactory.create(job=job, status=AnalysisTask.Status.SUCCESS)
        ExtractionTaskFactory.create(job=job, status=AnalysisTask.Status.SUCCESS)

        result = job.update_job_state()

        job.refresh_from_db()

        assert result is True
        assert job.status == AnalysisJob.Status.SUCCESS
        assert job.message == "All tasks succeeded."
        assert job.ended_at is not None

    @pytest.mark.django_db
    def test_job_update_job_state_some_tasks_fail(self):
        user = UserFactory.create()
        job = ExtractionJobFactory.create(owner=user, status=AnalysisJob.Status.PENDING)

        ExtractionTaskFactory.create(job=job, status=AnalysisTask.Status.SUCCESS)
        ExtractionTaskFactory.create(job=job, status=AnalysisTask.Status.FAILURE)
        ExtractionTaskFactory.create(job=job, status=AnalysisTask.Status.SUCCESS)

        result = job.update_job_state()
        job.refresh_from_db()

        assert result is True
        assert job.status == AnalysisJob.Status.FAILURE
        assert job.message == "Some tasks failed."
        assert job.ended_at is not None

    @pytest.mark.django_db
    def test_job_update_job_state_all_tasks_fail(self):
        user = UserFactory.create()
        job = ExtractionJobFactory.create(owner=user, status=AnalysisJob.Status.PENDING)

        ExtractionTaskFactory.create(job=job, status=AnalysisTask.Status.FAILURE)
        ExtractionTaskFactory.create(job=job, status=AnalysisTask.Status.FAILURE)

        result = job.update_job_state()
        job.refresh_from_db()

        assert result is True
        assert job.status == AnalysisJob.Status.FAILURE
        assert job.message == "All tasks failed."
        assert job.ended_at is not None

    @pytest.mark.django_db
    def test_job_update_job_state_tasks_still_pending(self):
        user = UserFactory.create()
        job = ExtractionJobFactory.create(owner=user, status=AnalysisJob.Status.PENDING)

        ExtractionTaskFactory.create(job=job, status=AnalysisTask.Status.SUCCESS)
        ExtractionTaskFactory.create(job=job, status=AnalysisTask.Status.PENDING)
        ExtractionTaskFactory.create(job=job, status=AnalysisTask.Status.FAILURE)

        result = job.update_job_state()
        job.refresh_from_db()

        assert result is False
        assert job.status == AnalysisJob.Status.PENDING
        assert job.ended_at is None

    @pytest.mark.django_db
    def test_job_update_job_state_tasks_in_progress(self):
        user = UserFactory.create()
        job = ExtractionJobFactory.create(owner=user, status=AnalysisJob.Status.PENDING)

        ExtractionTaskFactory.create(job=job, status=AnalysisTask.Status.SUCCESS)
        ExtractionTaskFactory.create(job=job, status=AnalysisTask.Status.IN_PROGRESS)

        result = job.update_job_state()
        job.refresh_from_db()

        assert result is False
        assert job.status == AnalysisJob.Status.IN_PROGRESS
        assert job.ended_at is None

    @pytest.mark.django_db
    def test_job_update_job_state_all_tasks_warning(self):
        user = UserFactory.create()
        job = ExtractionJobFactory.create(owner=user, status=AnalysisJob.Status.PENDING)

        ExtractionTaskFactory.create(job=job, status=AnalysisTask.Status.WARNING)
        ExtractionTaskFactory.create(job=job, status=AnalysisTask.Status.WARNING)

        result = job.update_job_state()
        job.refresh_from_db()

        assert result is True
        assert job.status == AnalysisJob.Status.WARNING
        assert job.message == "All tasks have warnings."
        assert job.ended_at is not None

    @pytest.mark.django_db
    def test_job_update_job_state_success_and_warning(self):
        user = UserFactory.create()
        job = ExtractionJobFactory.create(owner=user, status=AnalysisJob.Status.PENDING)

        ExtractionTaskFactory.create(job=job, status=AnalysisTask.Status.SUCCESS)
        ExtractionTaskFactory.create(job=job, status=AnalysisTask.Status.WARNING)

        result = job.update_job_state()
        job.refresh_from_db()

        assert result is True
        assert job.status == AnalysisJob.Status.WARNING
        assert job.message == "Some tasks have warnings."
        assert job.ended_at is not None

    @pytest.mark.django_db
    def test_job_update_job_state_warning_and_failure(self):
        user = UserFactory.create()
        job = ExtractionJobFactory.create(owner=user, status=AnalysisJob.Status.PENDING)

        ExtractionTaskFactory.create(job=job, status=AnalysisTask.Status.WARNING)
        ExtractionTaskFactory.create(job=job, status=AnalysisTask.Status.FAILURE)

        result = job.update_job_state()
        job.refresh_from_db()

        assert result is True
        assert job.status == AnalysisJob.Status.FAILURE
        assert job.message == "Some tasks failed."
        assert job.ended_at is not None

    @pytest.mark.django_db
    def test_job_update_job_state_success_warning_failure(self):
        user = UserFactory.create()
        job = ExtractionJobFactory.create(owner=user, status=AnalysisJob.Status.PENDING)

        # Mix of all states
        ExtractionTaskFactory.create(job=job, status=AnalysisTask.Status.SUCCESS)
        ExtractionTaskFactory.create(job=job, status=AnalysisTask.Status.WARNING)
        ExtractionTaskFactory.create(job=job, status=AnalysisTask.Status.FAILURE)

        result = job.update_job_state()
        job.refresh_from_db()

        assert result is True
        assert job.status == AnalysisJob.Status.FAILURE
        assert job.message == "Some tasks failed."
        assert job.ended_at is not None

    @pytest.mark.django_db
    def test_job_update_job_state_canceling_status(self):
        user = UserFactory.create()
        job = ExtractionJobFactory.create(owner=user, status=AnalysisJob.Status.CANCELING)

        ExtractionTaskFactory.create(job=job, status=AnalysisTask.Status.SUCCESS)
        ExtractionTaskFactory.create(job=job, status=AnalysisTask.Status.FAILURE)

        result = job.update_job_state()
        job.refresh_from_db()

        assert result is False
        assert job.status == AnalysisJob.Status.CANCELED
        assert job.ended_at is None

    @pytest.mark.django_db
    def test_job_update_job_state_no_tasks_remaining(self):
        user = UserFactory.create()
        job = ExtractionJobFactory.create(owner=user, status=AnalysisJob.Status.PENDING)

        # Job with no tasks should be marked as canceled
        result = job.update_job_state()
        job.refresh_from_db()

        assert result is False
        assert job.status == AnalysisJob.Status.CANCELED
        assert job.message == "No tasks remaining."

    @pytest.mark.django_db
    @time_machine.travel("2025-01-15 14:30:00+00:00")
    def test_job_timezone_correctness(self):
        user = UserFactory.create()
        job = ExtractionJobFactory.create(owner=user, status=AnalysisJob.Status.PENDING)

        ExtractionTaskFactory.create(job=job, status=AnalysisTask.Status.SUCCESS)

        job.update_job_state()
        job.refresh_from_db()

        expected_time = timezone.now()
        assert job.ended_at is not None
        assert abs((job.ended_at - expected_time).total_seconds()) < 1

    @pytest.mark.django_db
    @time_machine.travel("2025-03-20 09:15:30+01:00")
    def test_job_timezone_with_different_timezone(self):
        user = UserFactory.create()
        job = ExtractionJobFactory.create(owner=user, status=AnalysisJob.Status.PENDING)
        ExtractionTaskFactory.create(job=job, status=AnalysisTask.Status.SUCCESS)

        job.update_job_state()
        job.refresh_from_db()

        assert job.ended_at is not None
        expected_time = timezone.now()
        assert abs((job.ended_at - expected_time).total_seconds()) < 1

    @pytest.mark.django_db
    def test_job_update_job_state_sends_email_when_enabled(self):
        user = UserFactory.create()
        with patch.object(AnalysisJob, "_send_job_finished_mail") as mock_send_mail:
            job = ExtractionJobFactory.create(
                owner=user, status=AnalysisJob.Status.PENDING, send_finished_mail=True
            )
            ExtractionTaskFactory.create(job=job, status=AnalysisTask.Status.SUCCESS)

            result = job.update_job_state()

            assert result is True
            mock_send_mail.assert_called_once()

    @pytest.mark.django_db
    def test_job_update_job_state_does_not_send_email_when_disabled(self):
        user = UserFactory.create()
        with patch.object(AnalysisJob, "_send_job_finished_mail") as mock_send_mail:
            job = ExtractionJobFactory.create(
                owner=user, status=AnalysisJob.Status.PENDING, send_finished_mail=False
            )
            ExtractionTaskFactory.create(job=job, status=AnalysisTask.Status.SUCCESS)

            result = job.update_job_state()

            assert result is True
            mock_send_mail.assert_not_called()

    @pytest.mark.django_db
    def test_job_properties(self):
        user = UserFactory.create()
        job = ExtractionJobFactory.create(owner=user, status=AnalysisJob.Status.UNVERIFIED)
        assert not job.is_verified
        assert job.is_deletable

        job.status = AnalysisJob.Status.PREPARING
        assert job.is_preparing
        assert job.is_verified
        assert job.is_deletable
        assert job.is_cancelable

        job.status = AnalysisJob.Status.PENDING
        assert job.is_verified
        assert job.is_deletable
        assert job.is_cancelable
        assert not job.is_resumable
        assert not job.is_retriable
        assert not job.is_restartable

        job.status = AnalysisJob.Status.IN_PROGRESS
        assert job.is_cancelable

        job.status = AnalysisJob.Status.CANCELED
        assert job.is_resumable
        assert job.is_restartable

        job.status = AnalysisJob.Status.FAILURE
        assert job.is_retriable
        assert job.is_restartable

        job.status = AnalysisJob.Status.SUCCESS
        assert job.is_restartable

        job.status = AnalysisJob.Status.WARNING
        assert job.is_restartable

    @pytest.mark.django_db
    def test_job_is_deletable_with_non_pending_tasks(self):
        user = UserFactory.create()
        job = ExtractionJobFactory.create(owner=user, status=AnalysisJob.Status.PENDING)

        ExtractionTaskFactory.create(job=job, status=AnalysisTask.Status.SUCCESS)
        assert not job.is_deletable

        ExtractionTaskFactory.create(job=job, status=AnalysisTask.Status.PENDING)
        assert not job.is_deletable

    @pytest.mark.django_db
    def test_job_processed_tasks_property(self):
        user = UserFactory.create()
        job = ExtractionJobFactory.create(owner=user)

        pending_task = ExtractionTaskFactory.create(job=job, status=AnalysisTask.Status.PENDING)
        in_progress_task = ExtractionTaskFactory.create(
            job=job, status=AnalysisTask.Status.IN_PROGRESS
        )
        success_task = ExtractionTaskFactory.create(job=job, status=AnalysisTask.Status.SUCCESS)
        failure_task = ExtractionTaskFactory.create(job=job, status=AnalysisTask.Status.FAILURE)
        warning_task = ExtractionTaskFactory.create(job=job, status=AnalysisTask.Status.WARNING)
        canceled_task = ExtractionTaskFactory.create(job=job, status=AnalysisTask.Status.CANCELED)

        processed_tasks = job.processed_tasks

        assert pending_task not in processed_tasks
        assert in_progress_task not in processed_tasks

        assert success_task in processed_tasks
        assert failure_task in processed_tasks
        assert warning_task in processed_tasks
        assert canceled_task in processed_tasks

        assert processed_tasks.count() == 4

    @pytest.mark.django_db
    def test_job_reset_tasks_all_tasks(self):
        user = UserFactory.create()
        job = ExtractionJobFactory.create(owner=user)

        ExtractionTaskFactory.create(job=job, status=AnalysisTask.Status.SUCCESS)
        ExtractionTaskFactory.create(job=job, status=AnalysisTask.Status.FAILURE)
        ExtractionTaskFactory.create(job=job, status=AnalysisTask.Status.WARNING)

        with patch("radis.core.models.reset_tasks") as mock_reset_tasks:
            reset_tasks = job.reset_tasks()

            # Should reset all tasks
            mock_reset_tasks.assert_called_once()
            called_queryset = mock_reset_tasks.call_args[0][0]
            assert called_queryset.count() == 3
            assert reset_tasks.count() == 3

    @pytest.mark.django_db
    def test_job_reset_tasks_only_failed(self):
        user = UserFactory.create()
        job = ExtractionJobFactory.create(owner=user)

        ExtractionTaskFactory.create(job=job, status=AnalysisTask.Status.SUCCESS)
        task2 = ExtractionTaskFactory.create(job=job, status=AnalysisTask.Status.FAILURE)
        ExtractionTaskFactory.create(job=job, status=AnalysisTask.Status.WARNING)

        with patch("radis.core.models.reset_tasks") as mock_reset_tasks:
            reset_tasks = job.reset_tasks(only_failed=True)

            # Should only reset failed tasks
            mock_reset_tasks.assert_called_once()
            called_queryset = mock_reset_tasks.call_args[0][0]
            assert list(called_queryset) == [task2]
            assert reset_tasks.count() == 1

    @pytest.mark.django_db
    def test_job_string_representation(self):
        user = UserFactory.create()
        job = ExtractionJobFactory.create(owner=user)
        expected = f"ExtractionJob [{job.pk}]"
        assert str(job) == expected

    @pytest.mark.django_db
    def test_job_send_finished_mail_no_template(self):
        user = UserFactory.create()
        job = ExtractionJobFactory.create(owner=user)
        job.finished_mail_template = None

        with pytest.raises(ValueError, match="No finished mail template"):
            job._send_job_finished_mail()

    @pytest.mark.django_db
    def test_job_send_finished_mail_success(self):
        user = UserFactory.create(email="test@example.com")
        job = ExtractionJobFactory.create(owner=user)

        with (
            patch("radis.core.models.send_mail") as mock_send_mail,
            patch("radis.core.models.render_to_string") as mock_render,
            patch("radis.core.models.strip_tags") as mock_strip_tags,
        ):
            mock_render.return_value = "<html>Test email</html>"
            mock_strip_tags.return_value = "Test email"

            job._send_job_finished_mail()

            mock_render.assert_called_once()
            mock_strip_tags.assert_called_once_with("<html>Test email</html>")
            mock_send_mail.assert_called_once()

            call_args = mock_send_mail.call_args
            subject, plain_content, from_email, recipients = call_args[0]
            kwargs = call_args[1]

            assert subject == f"Job {job} finished"
            assert plain_content == "Test email"
            assert recipients == ["test@example.com"]
            assert kwargs["html_message"] == "<html>Test email</html>"

    @pytest.mark.django_db
    def test_job_get_mail_context_default(self):
        user = UserFactory.create()
        job = ExtractionJobFactory.create(owner=user)
        context = job.get_mail_context()
        assert context == {}  # Default implementation returns empty dict


class TestAnalysisTask:
    @pytest.mark.django_db
    def test_task_properties(self):
        user = UserFactory.create()
        job = ExtractionJobFactory.create(owner=user)
        task = ExtractionTaskFactory.create(job=job, status=AnalysisTask.Status.PENDING)
        assert task.is_deletable
        assert not task.is_resettable

        task.status = AnalysisTask.Status.IN_PROGRESS
        assert not task.is_deletable
        assert not task.is_resettable

        task.status = AnalysisTask.Status.SUCCESS
        assert not task.is_deletable
        assert task.is_resettable

        task.status = AnalysisTask.Status.FAILURE
        assert not task.is_deletable
        assert task.is_resettable

        task.status = AnalysisTask.Status.WARNING
        assert not task.is_deletable
        assert task.is_resettable

        task.status = AnalysisTask.Status.CANCELED
        assert not task.is_deletable
        assert task.is_resettable

    @pytest.mark.django_db
    def test_task_string_representation(self):
        user = UserFactory.create()
        job = ExtractionJobFactory.create(owner=user)
        task = ExtractionTaskFactory.create(job=job)
        expected = f"ExtractionTask [{task.pk}]"
        assert str(task) == expected

    @pytest.mark.django_db
    def test_task_is_queued_property(self):
        user = UserFactory.create()
        job = ExtractionJobFactory.create(owner=user)
        task = ExtractionTaskFactory.create(job=job, queued_job_id=None)
        assert not task.is_queued

        task.queued_job_id = 123
        assert task.is_queued
