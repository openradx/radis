from unittest.mock import patch

import pytest
from adit_radis_shared.accounts.factories import UserFactory
from django.test import Client

from radis.labels.models import LabelBackfillJob, LabelGroup, LabelQuestion, ReportLabel
from radis.labels.tasks import _maybe_finalize
from radis.reports.models import Language, Report


def _make_report(document_id: str = "doc-1") -> Report:
    """Create a minimal Report for backfill tests."""
    lang, _ = Language.objects.get_or_create(code="en")
    return Report.objects.create(
        document_id=document_id,
        body="Test report body",
        patient_birth_date="2000-01-01",
        patient_sex="M",
        study_datetime="2024-01-15T10:00:00Z",
        language=lang,
    )


def _make_question(group: LabelGroup, label: str) -> LabelQuestion:
    """Create a question and its default 'unknown' choice for label assignment."""
    with patch("radis.labels.signals.enqueue_label_group_backfill"):
        question = LabelQuestion.objects.create(group=group, label=label)
    return question


def _label_report_for_question(report: Report, question: LabelQuestion) -> ReportLabel:
    """Persist a ReportLabel using the question's first choice."""
    choice = question.choices.first()
    assert choice is not None, "Default choices should be created by signal"
    return ReportLabel.objects.create(report=report, question=question, choice=choice)

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

    def test_is_retryable_for_terminal_states(self):
        for status in [
            LabelBackfillJob.Status.FAILURE,
            LabelBackfillJob.Status.CANCELED,
            LabelBackfillJob.Status.SUCCESS,
        ]:
            job = self._create_job(status=status)
            assert job.is_retryable is True, f"Expected is_retryable=True for status={status}"

    def test_is_not_retryable_for_active_states(self):
        for status in [
            LabelBackfillJob.Status.PENDING,
            LabelBackfillJob.Status.IN_PROGRESS,
            LabelBackfillJob.Status.CANCELING,
        ]:
            job = self._create_job(status=status)
            assert job.is_retryable is False, f"Expected is_retryable=False for status={status}"

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


# -- LabelGroup.missing_reports tests --


@pytest.mark.django_db
class TestLabelGroupMissingReports:
    def test_empty_group_returns_no_missing_reports(self):
        # No active questions => nothing the system can label => empty result
        group = LabelGroup.objects.create(name="EmptyGroup")
        _make_report("doc-1")
        assert list(group.missing_reports()) == []

    def test_unlabelled_report_is_missing(self):
        group = LabelGroup.objects.create(name="G")
        _make_question(group, "Q1")
        report = _make_report("doc-1")
        assert list(group.missing_reports()) == [report]

    def test_fully_labelled_report_is_not_missing(self):
        group = LabelGroup.objects.create(name="G")
        question = _make_question(group, "Q1")
        report = _make_report("doc-1")
        _label_report_for_question(report, question)
        assert list(group.missing_reports()) == []

    def test_partially_labelled_report_is_missing(self):
        group = LabelGroup.objects.create(name="G")
        q1 = _make_question(group, "Q1")
        _make_question(group, "Q2")
        report = _make_report("doc-1")
        _label_report_for_question(report, q1)
        # Has a label for Q1 but not Q2 => still missing
        assert list(group.missing_reports()) == [report]

    def test_inactive_questions_do_not_count_toward_completion(self):
        group = LabelGroup.objects.create(name="G")
        active_q = _make_question(group, "Q1")
        # Create an inactive question that has no labels — should not affect missing
        with patch("radis.labels.signals.enqueue_label_group_backfill"):
            LabelQuestion.objects.create(group=group, label="QInactive", is_active=False)
        report = _make_report("doc-1")
        _label_report_for_question(report, active_q)
        # Only the active question matters; report is fully labelled wrt active questions
        assert list(group.missing_reports()) == []

    def test_label_for_other_group_does_not_count(self):
        group_a = LabelGroup.objects.create(name="GroupA")
        question_a = _make_question(group_a, "QA")
        group_b = LabelGroup.objects.create(name="GroupB")
        _make_question(group_b, "QB")
        report = _make_report("doc-1")
        # Label exists for group A, not group B => report still missing for group B
        _label_report_for_question(report, question_a)
        assert list(group_b.missing_reports()) == [report]


# -- _maybe_finalize tests --


@pytest.mark.django_db
class TestMaybeFinalize:
    def _setup_in_progress(
        self,
        with_question: bool = True,
        status=LabelBackfillJob.Status.IN_PROGRESS,
        total: int = 100,
    ) -> tuple[LabelBackfillJob, LabelGroup]:
        group = LabelGroup.objects.create(name="G")
        if with_question:
            _make_question(group, "Q1")
        job = LabelBackfillJob.objects.create(
            label_group=group,
            status=status,
            total_reports=total,
            processed_reports=0,
        )
        return job, group

    def test_noop_for_terminal_status(self):
        job, _ = self._setup_in_progress(
            with_question=False, status=LabelBackfillJob.Status.SUCCESS
        )
        _maybe_finalize(job.id)
        job.refresh_from_db()
        assert job.status == LabelBackfillJob.Status.SUCCESS
        assert job.ended_at is None

    def test_noop_when_missing_reports_remain(self):
        job, _ = self._setup_in_progress(with_question=True)
        _make_report("doc-1")  # unlabelled => missing
        _maybe_finalize(job.id)
        job.refresh_from_db()
        assert job.status == LabelBackfillJob.Status.IN_PROGRESS
        assert job.ended_at is None

    def test_finalizes_to_success_when_no_missing_reports(self):
        job, group = self._setup_in_progress(with_question=True)
        question = group.questions.first()
        assert question is not None
        report = _make_report("doc-1")
        _label_report_for_question(report, question)
        _maybe_finalize(job.id)
        job.refresh_from_db()
        assert job.status == LabelBackfillJob.Status.SUCCESS
        assert job.ended_at is not None
        assert job.processed_reports == job.total_reports

    def test_finalizes_to_canceled_from_canceling(self):
        job, _ = self._setup_in_progress(
            with_question=False, status=LabelBackfillJob.Status.CANCELING
        )
        _maybe_finalize(job.id)
        job.refresh_from_db()
        assert job.status == LabelBackfillJob.Status.CANCELED
        assert job.ended_at is not None

    def test_handles_missing_job_gracefully(self):
        # Should not raise
        _maybe_finalize(99999)

    def test_concurrent_cancel_is_not_overwritten(self):
        """If status flips to CANCELING between read and write, finalize as CANCELED."""
        job, _ = self._setup_in_progress(with_question=False)  # no work, can finalize

        # Patch the in-memory read so the helper sees IN_PROGRESS,
        # then the DB row transitions to CANCELING before the UPDATE fires.
        original_get = LabelBackfillJob.objects.get

        def get_then_cancel(*args, **kwargs):
            job_obj = original_get(*args, **kwargs)
            # Simulate a concurrent cancel write hitting the DB
            LabelBackfillJob.objects.filter(id=job_obj.id).update(
                status=LabelBackfillJob.Status.CANCELING
            )
            return job_obj

        with patch.object(LabelBackfillJob.objects, "get", side_effect=get_then_cancel):
            _maybe_finalize(job.id)

        job.refresh_from_db()
        # The conditional UPDATEs should pick the CANCELING branch and set CANCELED,
        # not silently overwrite with SUCCESS.
        assert job.status == LabelBackfillJob.Status.CANCELED


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
        user = UserFactory.create(is_active=True, is_staff=True)
        client.force_login(user)
        job = self._create_job(status=LabelBackfillJob.Status.IN_PROGRESS)

        response = client.post(f"/labels/backfill/{job.pk}/cancel/")
        assert response.status_code == 302

        job.refresh_from_db()
        assert job.status == LabelBackfillJob.Status.CANCELING

    def test_cancel_pending_job(self, client: Client):
        user = UserFactory.create(is_active=True, is_staff=True)
        client.force_login(user)
        job = self._create_job(status=LabelBackfillJob.Status.PENDING)

        response = client.post(f"/labels/backfill/{job.pk}/cancel/")
        assert response.status_code == 302

        job.refresh_from_db()
        assert job.status == LabelBackfillJob.Status.CANCELING

    def test_cancel_rejected_for_non_staff(self, client: Client):
        user = UserFactory.create(is_active=True, is_staff=False)
        client.force_login(user)
        job = self._create_job(status=LabelBackfillJob.Status.IN_PROGRESS)

        response = client.post(f"/labels/backfill/{job.pk}/cancel/")
        assert response.status_code == 403

        job.refresh_from_db()
        assert job.status == LabelBackfillJob.Status.IN_PROGRESS

    def test_cancel_already_completed_returns_400(self, client: Client):
        user = UserFactory.create(is_active=True, is_staff=True)
        client.force_login(user)
        job = self._create_job(status=LabelBackfillJob.Status.SUCCESS)

        response = client.post(f"/labels/backfill/{job.pk}/cancel/")
        assert response.status_code == 400

    def test_cancel_nonexistent_job_returns_404(self, client: Client):
        user = UserFactory.create(is_active=True, is_staff=True)
        client.force_login(user)

        response = client.post("/labels/backfill/99999/cancel/")
        assert response.status_code == 404

    def test_cancel_redirects_to_group_detail(self, client: Client):
        user = UserFactory.create(is_active=True, is_staff=True)
        client.force_login(user)
        job = self._create_job()

        response = client.post(f"/labels/backfill/{job.pk}/cancel/")
        assert response.status_code == 302
        assert f"/labels/{job.label_group_id}/" in response["Location"]

    def test_get_not_allowed(self, client: Client):
        user = UserFactory.create(is_active=True, is_staff=True)
        client.force_login(user)
        job = self._create_job()

        response = client.get(f"/labels/backfill/{job.pk}/cancel/")
        assert response.status_code == 405


# -- Retry view tests --


@pytest.mark.django_db
class TestLabelBackfillRetryView:
    def _create_job(self, status=LabelBackfillJob.Status.FAILURE) -> LabelBackfillJob:
        group = LabelGroup.objects.create(name="TestGroup")
        return LabelBackfillJob.objects.create(
            label_group=group,
            status=status,
            total_reports=100,
            processed_reports=80,
        )

    def test_retry_requires_login(self, client: Client):
        job = self._create_job()
        response = client.post(f"/labels/backfill/{job.pk}/retry/")
        assert response.status_code == 302
        assert "/accounts/login/" in response["Location"]

    def test_retry_rejected_for_non_staff(self, client: Client):
        user = UserFactory.create(is_active=True, is_staff=False)
        client.force_login(user)
        job = self._create_job()

        response = client.post(f"/labels/backfill/{job.pk}/retry/")
        assert response.status_code == 403

        job.refresh_from_db()
        assert job.status == LabelBackfillJob.Status.FAILURE

    def test_retry_resets_failed_job_to_pending(self, client: Client):
        user = UserFactory.create(is_active=True, is_staff=True)
        client.force_login(user)
        job = self._create_job(status=LabelBackfillJob.Status.FAILURE)

        with patch("radis.labels.views.enqueue_label_group_backfill") as mock_task:
            mock_task.defer = lambda **kw: None
            response = client.post(f"/labels/backfill/{job.pk}/retry/")

        assert response.status_code == 302
        job.refresh_from_db()
        assert job.status == LabelBackfillJob.Status.PENDING
        assert job.started_at is None
        assert job.ended_at is None

    def test_retry_canceled_job_to_pending(self, client: Client):
        user = UserFactory.create(is_active=True, is_staff=True)
        client.force_login(user)
        job = self._create_job(status=LabelBackfillJob.Status.CANCELED)

        with patch("radis.labels.views.enqueue_label_group_backfill") as mock_task:
            mock_task.defer = lambda **kw: None
            client.post(f"/labels/backfill/{job.pk}/retry/")

        job.refresh_from_db()
        assert job.status == LabelBackfillJob.Status.PENDING

    def test_retry_in_progress_job_returns_400(self, client: Client):
        user = UserFactory.create(is_active=True, is_staff=True)
        client.force_login(user)
        job = self._create_job(status=LabelBackfillJob.Status.IN_PROGRESS)

        response = client.post(f"/labels/backfill/{job.pk}/retry/")
        assert response.status_code == 400

        job.refresh_from_db()
        assert job.status == LabelBackfillJob.Status.IN_PROGRESS

    def test_retry_skipped_when_other_active_backfill_exists(self, client: Client):
        user = UserFactory.create(is_active=True, is_staff=True)
        client.force_login(user)

        group = LabelGroup.objects.create(name="TestGroup")
        failed_job = LabelBackfillJob.objects.create(
            label_group=group,
            status=LabelBackfillJob.Status.FAILURE,
            total_reports=100,
        )
        # Another backfill for the same group is already active
        LabelBackfillJob.objects.create(
            label_group=group,
            status=LabelBackfillJob.Status.IN_PROGRESS,
            total_reports=100,
        )

        with patch("radis.labels.views.enqueue_label_group_backfill") as mock_task:
            response = client.post(f"/labels/backfill/{failed_job.pk}/retry/")

        assert response.status_code == 302
        # The failed job should NOT be reset, since the other backfill will pick up missing labels
        failed_job.refresh_from_db()
        assert failed_job.status == LabelBackfillJob.Status.FAILURE
        # Coordinator should not have been deferred
        assert not mock_task.defer.called

    def test_retry_get_not_allowed(self, client: Client):
        user = UserFactory.create(is_active=True, is_staff=True)
        client.force_login(user)
        job = self._create_job()

        response = client.get(f"/labels/backfill/{job.pk}/retry/")
        assert response.status_code == 405

    def test_retry_nonexistent_job_returns_404(self, client: Client):
        user = UserFactory.create(is_active=True, is_staff=True)
        client.force_login(user)

        response = client.post("/labels/backfill/99999/retry/")
        assert response.status_code == 404


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
