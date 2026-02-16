from unittest.mock import patch

import pytest
from adit_radis_shared.accounts.factories import UserFactory
from django.test import Client

from radis.labels.models import LabelBackfillJob, LabelGroup, LabelQuestion
from radis.labels.tasks import _increment_and_maybe_finalize

# -- Model tests --


@pytest.mark.django_db
class TestLabelBackfillJobModel:
    def _create_job(self, **kwargs) -> LabelBackfillJob:
        group = LabelGroup.objects.create(name="Findings")
        defaults = {"label_group": group}
        defaults.update(kwargs)
        return LabelBackfillJob.objects.create(**defaults)

    def test_default_status_is_pending(self):
        job = self._create_job()
        assert job.status == LabelBackfillJob.Status.PENDING

    def test_str(self):
        job = self._create_job()
        assert str(job) == f"LabelBackfillJob [{job.pk}]"

    def test_is_cancelable_pending(self):
        job = self._create_job(status=LabelBackfillJob.Status.PENDING)
        assert job.is_cancelable is True

    def test_is_cancelable_in_progress(self):
        job = self._create_job(status=LabelBackfillJob.Status.IN_PROGRESS)
        assert job.is_cancelable is True

    def test_is_not_cancelable_success(self):
        job = self._create_job(status=LabelBackfillJob.Status.SUCCESS)
        assert job.is_cancelable is False

    def test_is_not_cancelable_canceled(self):
        job = self._create_job(status=LabelBackfillJob.Status.CANCELED)
        assert job.is_cancelable is False

    def test_is_not_cancelable_canceling(self):
        job = self._create_job(status=LabelBackfillJob.Status.CANCELING)
        assert job.is_cancelable is False

    def test_is_not_cancelable_failure(self):
        job = self._create_job(status=LabelBackfillJob.Status.FAILURE)
        assert job.is_cancelable is False

    def test_is_active_pending(self):
        job = self._create_job(status=LabelBackfillJob.Status.PENDING)
        assert job.is_active is True

    def test_is_active_in_progress(self):
        job = self._create_job(status=LabelBackfillJob.Status.IN_PROGRESS)
        assert job.is_active is True

    def test_is_not_active_terminal_states(self):
        for status in [
            LabelBackfillJob.Status.SUCCESS,
            LabelBackfillJob.Status.FAILURE,
            LabelBackfillJob.Status.CANCELED,
            LabelBackfillJob.Status.CANCELING,
        ]:
            job = self._create_job(status=status)
            assert job.is_active is False, f"Expected is_active=False for status={status}"

    def test_progress_percent_zero_total(self):
        job = self._create_job(total_reports=0, processed_reports=0)
        assert job.progress_percent == 0

    def test_progress_percent_partial(self):
        job = self._create_job(total_reports=200, processed_reports=50)
        assert job.progress_percent == 25

    def test_progress_percent_complete(self):
        job = self._create_job(total_reports=100, processed_reports=100)
        assert job.progress_percent == 100

    def test_progress_percent_capped_at_100(self):
        job = self._create_job(total_reports=100, processed_reports=150)
        assert job.progress_percent == 100

    def test_ordering_by_created_at_descending(self):
        group = LabelGroup.objects.create(name="TestGroup")
        job1 = LabelBackfillJob.objects.create(label_group=group)
        job2 = LabelBackfillJob.objects.create(label_group=group)
        jobs = list(LabelBackfillJob.objects.all())
        assert jobs[0] == job2
        assert jobs[1] == job1

    def test_cascade_delete_with_group(self):
        group = LabelGroup.objects.create(name="DeleteMe")
        LabelBackfillJob.objects.create(label_group=group)
        assert LabelBackfillJob.objects.count() == 1
        group.delete()
        assert LabelBackfillJob.objects.count() == 0


# -- Signal dedup tests --


@pytest.mark.django_db
class TestSignalDedup:
    @patch("radis.labels.signals.enqueue_label_group_backfill")
    def test_creating_question_creates_backfill_job(self, mock_task):
        mock_task.defer = lambda **kw: None
        group = LabelGroup.objects.create(name="Findings")
        LabelQuestion.objects.create(group=group, label="PE present?")

        assert LabelBackfillJob.objects.filter(label_group=group).count() == 1

    @patch("radis.labels.signals.enqueue_label_group_backfill")
    def test_second_question_skips_backfill_when_active(self, mock_task):
        mock_task.defer = lambda **kw: None
        group = LabelGroup.objects.create(name="Findings")
        LabelQuestion.objects.create(group=group, label="PE present?")
        LabelQuestion.objects.create(group=group, label="Pneumonia present?")

        # Only one backfill job should exist
        assert LabelBackfillJob.objects.filter(label_group=group).count() == 1

    @patch("radis.labels.signals.enqueue_label_group_backfill")
    def test_new_question_after_completed_backfill_creates_new_job(self, mock_task):
        mock_task.defer = lambda **kw: None
        group = LabelGroup.objects.create(name="Findings")
        LabelQuestion.objects.create(group=group, label="PE present?")

        # Simulate first backfill completing
        job = LabelBackfillJob.objects.get(label_group=group)
        job.status = LabelBackfillJob.Status.SUCCESS
        job.save()

        LabelQuestion.objects.create(group=group, label="Pneumonia present?")
        assert LabelBackfillJob.objects.filter(label_group=group).count() == 2

    @patch("radis.labels.signals.enqueue_label_group_backfill")
    def test_inactive_question_does_not_trigger_backfill(self, mock_task):
        mock_task.defer = lambda **kw: None
        group = LabelGroup.objects.create(name="Findings")
        LabelQuestion.objects.create(group=group, label="Draft Q", is_active=False)

        assert LabelBackfillJob.objects.filter(label_group=group).count() == 0

    @patch("radis.labels.signals.enqueue_label_group_backfill")
    def test_updating_question_does_not_trigger_backfill(self, mock_task):
        mock_task.defer = lambda **kw: None
        group = LabelGroup.objects.create(name="Findings")
        question = LabelQuestion.objects.create(group=group, label="PE present?")

        # Clear the job created by the initial create
        LabelBackfillJob.objects.all().delete()

        question.label = "Updated label"
        question.save()
        assert LabelBackfillJob.objects.filter(label_group=group).count() == 0

    @patch("radis.labels.signals.enqueue_label_group_backfill")
    @pytest.mark.django_db(transaction=True)
    def test_dedup_across_different_groups(self, mock_task):
        mock_task.defer = lambda **kw: None
        group1 = LabelGroup.objects.create(name="Group A")
        group2 = LabelGroup.objects.create(name="Group B")

        LabelQuestion.objects.create(group=group1, label="Q1")
        LabelQuestion.objects.create(group=group2, label="Q2")

        # Each group should get its own backfill
        assert LabelBackfillJob.objects.filter(label_group=group1).count() == 1
        assert LabelBackfillJob.objects.filter(label_group=group2).count() == 1


# -- increment_and_maybe_finalize tests --


@pytest.mark.django_db
class TestIncrementAndMaybeFinalize:
    def _create_active_job(self, total=100, processed=0) -> LabelBackfillJob:
        group = LabelGroup.objects.create(name="TestGroup")
        return LabelBackfillJob.objects.create(
            label_group=group,
            status=LabelBackfillJob.Status.IN_PROGRESS,
            total_reports=total,
            processed_reports=processed,
        )

    def test_increments_processed_reports(self):
        job = self._create_active_job(total=100, processed=0)
        _increment_and_maybe_finalize(job.id, 25)
        job.refresh_from_db()
        assert job.processed_reports == 25

    def test_does_not_finalize_when_incomplete(self):
        job = self._create_active_job(total=100, processed=0)
        _increment_and_maybe_finalize(job.id, 25)
        job.refresh_from_db()
        assert job.status == LabelBackfillJob.Status.IN_PROGRESS
        assert job.ended_at is None

    def test_finalizes_to_success_when_complete(self):
        job = self._create_active_job(total=100, processed=75)
        _increment_and_maybe_finalize(job.id, 25)
        job.refresh_from_db()
        assert job.status == LabelBackfillJob.Status.SUCCESS
        assert job.ended_at is not None

    def test_finalizes_to_canceled_when_canceling(self):
        group = LabelGroup.objects.create(name="TestGroup")
        job = LabelBackfillJob.objects.create(
            label_group=group,
            status=LabelBackfillJob.Status.CANCELING,
            total_reports=100,
            processed_reports=75,
        )
        _increment_and_maybe_finalize(job.id, 25)
        job.refresh_from_db()
        assert job.status == LabelBackfillJob.Status.CANCELED
        assert job.ended_at is not None

    def test_handles_missing_job_gracefully(self):
        # Should not raise
        _increment_and_maybe_finalize(99999, 10)

    def test_over_counting_still_finalizes(self):
        job = self._create_active_job(total=100, processed=90)
        _increment_and_maybe_finalize(job.id, 20)
        job.refresh_from_db()
        assert job.processed_reports == 110
        assert job.status == LabelBackfillJob.Status.SUCCESS


# -- Cancel view tests --


@pytest.mark.django_db
class TestLabelBackfillCancelView:
    def _create_job(self, status=LabelBackfillJob.Status.IN_PROGRESS) -> LabelBackfillJob:
        group = LabelGroup.objects.create(name="TestGroup")
        return LabelBackfillJob.objects.create(
            label_group=group,
            status=status,
            total_reports=100,
        )

    def test_cancel_requires_login(self, client: Client):
        job = self._create_job()
        response = client.post(f"/labels/backfill/{job.pk}/cancel/")
        assert response.status_code == 302
        assert "/accounts/login/" in response["Location"]

    def test_cancel_sets_canceling_status(self, client: Client):
        user = UserFactory.create(is_active=True)
        client.force_login(user)
        job = self._create_job(status=LabelBackfillJob.Status.IN_PROGRESS)

        response = client.post(f"/labels/backfill/{job.pk}/cancel/")
        assert response.status_code == 302

        job.refresh_from_db()
        assert job.status == LabelBackfillJob.Status.CANCELING

    def test_cancel_pending_job(self, client: Client):
        user = UserFactory.create(is_active=True)
        client.force_login(user)
        job = self._create_job(status=LabelBackfillJob.Status.PENDING)

        response = client.post(f"/labels/backfill/{job.pk}/cancel/")
        assert response.status_code == 302

        job.refresh_from_db()
        assert job.status == LabelBackfillJob.Status.CANCELING

    def test_cancel_already_completed_returns_400(self, client: Client):
        user = UserFactory.create(is_active=True)
        client.force_login(user)
        job = self._create_job(status=LabelBackfillJob.Status.SUCCESS)

        response = client.post(f"/labels/backfill/{job.pk}/cancel/")
        assert response.status_code == 400

    def test_cancel_nonexistent_job_returns_404(self, client: Client):
        user = UserFactory.create(is_active=True)
        client.force_login(user)

        response = client.post("/labels/backfill/99999/cancel/")
        assert response.status_code == 404

    def test_cancel_redirects_to_group_detail(self, client: Client):
        user = UserFactory.create(is_active=True)
        client.force_login(user)
        job = self._create_job()

        response = client.post(f"/labels/backfill/{job.pk}/cancel/")
        assert response.status_code == 302
        assert f"/labels/{job.label_group_id}/" in response["Location"]

    def test_get_not_allowed(self, client: Client):
        user = UserFactory.create(is_active=True)
        client.force_login(user)
        job = self._create_job()

        response = client.get(f"/labels/backfill/{job.pk}/cancel/")
        assert response.status_code == 405


# -- Detail view context tests --


@pytest.mark.django_db
class TestLabelGroupDetailViewBackfill:
    def test_detail_view_includes_backfill_job(self, client: Client):
        user = UserFactory.create(is_active=True)
        client.force_login(user)

        group = LabelGroup.objects.create(name="Findings")
        job = LabelBackfillJob.objects.create(
            label_group=group,
            status=LabelBackfillJob.Status.IN_PROGRESS,
            total_reports=500,
            processed_reports=200,
        )

        response = client.get(f"/labels/{group.pk}/")
        assert response.status_code == 200
        assert response.context["backfill_job"] == job

    def test_detail_view_returns_most_recent_backfill(self, client: Client):
        user = UserFactory.create(is_active=True)
        client.force_login(user)

        group = LabelGroup.objects.create(name="Findings")
        LabelBackfillJob.objects.create(
            label_group=group,
            status=LabelBackfillJob.Status.SUCCESS,
        )
        job2 = LabelBackfillJob.objects.create(
            label_group=group,
            status=LabelBackfillJob.Status.IN_PROGRESS,
        )

        response = client.get(f"/labels/{group.pk}/")
        assert response.context["backfill_job"] == job2

    def test_detail_view_no_backfill_job(self, client: Client):
        user = UserFactory.create(is_active=True)
        client.force_login(user)

        group = LabelGroup.objects.create(name="Findings")
        response = client.get(f"/labels/{group.pk}/")
        assert response.context["backfill_job"] is None


# -- Management command tests --


@pytest.mark.django_db
class TestLabelsBackfillCommand:
    @patch("radis.labels.tasks.process_label_group")
    def test_command_creates_backfill_job(self, mock_task):
        mock_task.defer = lambda **kw: None
        from django.core.management import call_command

        from radis.reports.models import Language, Report

        group = LabelGroup.objects.create(name="Findings")
        lang = Language.objects.create(code="en")
        Report.objects.create(
            document_id="doc-1",
            body="Test report body",
            patient_birth_date="2000-01-01",
            patient_sex="M",
            study_datetime="2024-01-15T10:00:00Z",
            language=lang,
        )

        call_command("labels_backfill", group=str(group.id))

        job = LabelBackfillJob.objects.get(label_group=group)
        assert job.status == LabelBackfillJob.Status.IN_PROGRESS
        assert job.total_reports == 1
        assert job.started_at is not None


# -- Templatetag tests --


class TestBackfillStatusCssFilter:
    def test_pending(self):
        from radis.labels.templatetags.labels_extras import backfill_status_css

        assert backfill_status_css(LabelBackfillJob.Status.PENDING) == "text-secondary"

    def test_in_progress(self):
        from radis.labels.templatetags.labels_extras import backfill_status_css

        assert backfill_status_css(LabelBackfillJob.Status.IN_PROGRESS) == "text-info"

    def test_success(self):
        from radis.labels.templatetags.labels_extras import backfill_status_css

        assert backfill_status_css(LabelBackfillJob.Status.SUCCESS) == "text-success"

    def test_failure(self):
        from radis.labels.templatetags.labels_extras import backfill_status_css

        assert backfill_status_css(LabelBackfillJob.Status.FAILURE) == "text-danger"

    def test_canceling(self):
        from radis.labels.templatetags.labels_extras import backfill_status_css

        assert backfill_status_css(LabelBackfillJob.Status.CANCELING) == "text-muted"

    def test_canceled(self):
        from radis.labels.templatetags.labels_extras import backfill_status_css

        assert backfill_status_css(LabelBackfillJob.Status.CANCELED) == "text-muted"

    def test_unknown_status(self):
        from radis.labels.templatetags.labels_extras import backfill_status_css

        assert backfill_status_css("XX") == ""
