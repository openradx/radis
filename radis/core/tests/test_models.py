import datetime
from unittest.mock import ANY, patch

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
        # Verify that it normalized to UTC
        assert job.ended_at.tzinfo == datetime.timezone.utc
        # Verify the UTC conversion is correct
        expected_utc = expected_time.astimezone(datetime.timezone.utc)
        assert abs((job.ended_at - expected_utc).total_seconds()) < 1

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
            mock_send_mail.assert_called_once_with()  # Verify exact arguments (no args)

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

            # Verify render_to_string called with correct template and context
            mock_render.assert_called_once_with(
                job.finished_mail_template,
                {
                    "job": job,
                    **job.get_mail_context(),
                },
            )
            mock_strip_tags.assert_called_once_with("<html>Test email</html>")

            # Verify exact arguments passed to send_mail
            mock_send_mail.assert_called_once_with(
                f"Job {job} finished",
                "Test email",
                ANY,  # settings.DEFAULT_FROM_EMAIL
                ["test@example.com"],
                html_message="<html>Test email</html>",
            )

    @pytest.mark.django_db
    def test_job_get_mail_context_default(self):
        user = UserFactory.create()
        job = ExtractionJobFactory.create(owner=user)
        context = job.get_mail_context()
        assert context == {}  # Default implementation returns empty dict

    @pytest.mark.django_db
    def test_job_update_job_state_with_only_canceled_tasks(self):
        user = UserFactory.create()
        job = ExtractionJobFactory.create(owner=user, status=AnalysisJob.Status.PENDING)
        ExtractionTaskFactory.create(job=job, status=AnalysisTask.Status.CANCELED)
        ExtractionTaskFactory.create(job=job, status=AnalysisTask.Status.CANCELED)

        with pytest.raises(AssertionError, match="Invalid task status"):
            job.update_job_state()

    @pytest.mark.django_db
    def test_job_update_job_state_consecutive_calls(self):
        user = UserFactory.create()
        job = ExtractionJobFactory.create(owner=user, status=AnalysisJob.Status.PENDING)
        ExtractionTaskFactory.create(job=job, status=AnalysisTask.Status.SUCCESS)

        # First call should update the job to SUCCESS
        result1 = job.update_job_state()
        job.refresh_from_db()
        first_ended_at = job.ended_at

        assert result1 is True
        assert job.status == AnalysisJob.Status.SUCCESS
        assert first_ended_at is not None

        # Second call should not change anything since job is already finished
        result2 = job.update_job_state()
        job.refresh_from_db()
        second_ended_at = job.ended_at

        assert result2 is True
        assert job.status == AnalysisJob.Status.SUCCESS
        time_diff = abs((second_ended_at - first_ended_at).total_seconds())
        assert time_diff < 1.0  # Should be essentially the same time

    @pytest.mark.django_db
    def test_job_message_field_with_very_long_text(self):
        user = UserFactory.create()
        job = ExtractionJobFactory.create(owner=user)

        very_long_message = "A" * 10000
        job.message = very_long_message
        job.save()

        job.refresh_from_db()
        assert job.message == very_long_message

    @pytest.mark.django_db
    def test_job_message_field_with_unicode_characters(self):
        user = UserFactory.create()
        job = ExtractionJobFactory.create(owner=user)

        unicode_message = " ñoël <script>alert('xss')</script> «quotes» ½"
        job.message = unicode_message
        job.save()

        job.refresh_from_db()
        assert job.message == unicode_message

    @pytest.mark.django_db
    def test_job_message_field_with_empty_content(self):
        user = UserFactory.create()
        job = ExtractionJobFactory.create(owner=user)

        job.message = ""
        job.save()
        job.refresh_from_db()
        assert job.message == ""

        job.message = "   \t\n  "
        job.save()
        job.refresh_from_db()
        assert job.message == "   \t\n  "

    @pytest.mark.django_db
    def test_job_cancel_state_transitions(self):
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
    def test_job_property_consistency_with_status(self):
        user = UserFactory.create()
        job = ExtractionJobFactory.create(owner=user)

        status_property_mapping = {
            AnalysisJob.Status.UNVERIFIED: {
                "is_verified": False,
                "is_deletable": True,
                "is_cancelable": False,
                "is_resumable": False,
                "is_retriable": False,
                "is_restartable": False,
            },
            AnalysisJob.Status.PREPARING: {
                "is_verified": True,
                "is_deletable": True,
                "is_cancelable": True,
                "is_resumable": False,
                "is_retriable": False,
                "is_restartable": False,
            },
            AnalysisJob.Status.PENDING: {
                "is_verified": True,
                "is_deletable": True,
                "is_cancelable": True,
                "is_resumable": False,
                "is_retriable": False,
                "is_restartable": False,
            },
            AnalysisJob.Status.SUCCESS: {
                "is_verified": True,
                "is_deletable": False,
                "is_cancelable": False,
                "is_resumable": False,
                "is_retriable": False,
                "is_restartable": True,
            },
            AnalysisJob.Status.CANCELED: {
                "is_verified": True,
                "is_deletable": False,
                "is_cancelable": False,
                "is_resumable": True,
                "is_retriable": False,
                "is_restartable": True,
            },
        }

        for status, expected_properties in status_property_mapping.items():
            job.status = status
            job.save()

            for prop_name, expected_value in expected_properties.items():
                actual_value = getattr(job, prop_name)
                assert actual_value == expected_value, (
                    f"For status {status}, property {prop_name} should be {expected_value} "
                    f"but was {actual_value}"
                )


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

    @pytest.mark.django_db
    def test_task_timestamps_behavior(self):
        user = UserFactory.create()
        job = ExtractionJobFactory.create(owner=user)
        task = ExtractionTaskFactory.create(job=job)

        assert task.created_at is not None
        assert task.started_at is None
        assert task.ended_at is None

        now = timezone.now()
        task.started_at = now
        task.ended_at = now
        task.save()

        task.refresh_from_db()
        assert task.started_at == now
        assert task.ended_at == now

        task.created_at = None
        with pytest.raises(Exception):
            task.save()
