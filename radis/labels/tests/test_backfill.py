from unittest.mock import patch

import pytest
from adit_radis_shared.accounts.factories import UserFactory
from django.test import Client

from radis.labels.models import (
    Answer,
    BackfillJob,
    LabelingRun,
    Question,
    QuestionSet,
)
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


def _make_question(question_set: QuestionSet, label: str) -> Question:
    """Create a question (signal auto-creates the default answer options).

    Backfill scheduling no longer fires on save (it moved to the nightly
    launcher), so this helper no longer needs to patch the task module.
    """
    return Question.objects.create(question_set=question_set, label=label)


def _record_complete_runs(report: Report, question_set: QuestionSet) -> list[LabelingRun]:
    """Create a SUCCESS run + answers for every mode currently configured.

    This is the "report is fully labelled for this set" condition used
    throughout the missing-reports / finalize logic. ``missing_reports``
    requires *every* mode in ``settings.LABELS_RUN_MODES`` to have a SUCCESS
    run with answers for every active question, so tests that want a fully
    labelled report should call this helper, not record one mode by hand.
    """
    from django.conf import settings

    runs: list[LabelingRun] = []
    modes = getattr(settings, "LABELS_RUN_MODES", [LabelingRun.Mode.DIRECT])
    for mode in modes:
        run = LabelingRun.objects.create(
            report=report,
            question_set=question_set,
            mode=mode,
            status=LabelingRun.Status.SUCCESS,
        )
        for question in question_set.questions.filter(is_active=True):
            option = question.options.first()
            assert option is not None, "Default options should be created by signal"
            Answer.objects.create(
                run=run,
                report=report,
                question=question,
                question_version=question.version,
                option=option,
            )
        runs.append(run)
    return runs


# -- Model tests --


@pytest.mark.django_db
class TestBackfillJobModel:
    def _create_job(self, **kwargs) -> BackfillJob:
        question_set = QuestionSet.objects.create(name="Findings")
        defaults = {"question_set": question_set}
        defaults.update(kwargs)
        return BackfillJob.objects.create(**defaults)

    def test_default_status_is_pending(self):
        job = self._create_job()
        assert job.status == BackfillJob.Status.PENDING

    def test_str(self):
        job = self._create_job()
        assert str(job) == f"BackfillJob [{job.pk}]"

    def test_is_cancelable_pending(self):
        job = self._create_job(status=BackfillJob.Status.PENDING)
        assert job.is_cancelable is True

    def test_is_cancelable_in_progress(self):
        job = self._create_job(status=BackfillJob.Status.IN_PROGRESS)
        assert job.is_cancelable is True

    def test_is_not_cancelable_success(self):
        job = self._create_job(status=BackfillJob.Status.SUCCESS)
        assert job.is_cancelable is False

    def test_is_not_cancelable_canceled(self):
        job = self._create_job(status=BackfillJob.Status.CANCELED)
        assert job.is_cancelable is False

    def test_is_not_cancelable_failure(self):
        job = self._create_job(status=BackfillJob.Status.FAILURE)
        assert job.is_cancelable is False

    def test_is_active_pending(self):
        job = self._create_job(status=BackfillJob.Status.PENDING)
        assert job.is_active is True

    def test_is_active_in_progress(self):
        job = self._create_job(status=BackfillJob.Status.IN_PROGRESS)
        assert job.is_active is True

    def test_is_not_active_terminal_states(self):
        for status in [
            BackfillJob.Status.SUCCESS,
            BackfillJob.Status.FAILURE,
            BackfillJob.Status.CANCELED,
        ]:
            job = self._create_job(status=status)
            assert job.is_active is False, f"Expected is_active=False for status={status}"

    def test_is_retryable_for_terminal_states(self):
        for status in [
            BackfillJob.Status.FAILURE,
            BackfillJob.Status.CANCELED,
            BackfillJob.Status.SUCCESS,
        ]:
            job = self._create_job(status=status)
            assert job.is_retryable is True, f"Expected is_retryable=True for status={status}"

    def test_is_not_retryable_for_active_states(self):
        for status in [BackfillJob.Status.PENDING, BackfillJob.Status.IN_PROGRESS]:
            job = self._create_job(status=status)
            assert job.is_retryable is False, f"Expected is_retryable=False for status={status}"

    def test_progress_percent_zero_total(self):
        job = self._create_job(
            total_reports=0,
            processed_reports=0,
            status=BackfillJob.Status.SUCCESS,
        )
        assert job.progress_percent == 0

    def test_progress_percent_terminal_uses_snapshot(self):
        job = self._create_job(
            total_reports=200,
            processed_reports=50,
            status=BackfillJob.Status.CANCELED,
        )
        assert job.progress_percent == 25

    def test_progress_percent_complete_terminal(self):
        job = self._create_job(
            total_reports=100,
            processed_reports=100,
            status=BackfillJob.Status.SUCCESS,
        )
        assert job.progress_percent == 100

    def test_progress_percent_capped_at_100(self):
        job = self._create_job(
            total_reports=100,
            processed_reports=150,
            status=BackfillJob.Status.SUCCESS,
        )
        assert job.progress_percent == 100

    def test_progress_percent_active_derives_live(self):
        question_set = QuestionSet.objects.create(name="LiveProgress")
        _make_question(question_set, "Q1")
        labelled = _make_report("doc-1")
        _make_report("doc-2")
        _record_complete_runs(labelled, question_set)

        job = BackfillJob.objects.create(
            question_set=question_set,
            status=BackfillJob.Status.IN_PROGRESS,
            total_reports=2,
            processed_reports=0,
        )
        # 1 of 2 reports has DIRECT runs => 50%
        assert job.processed_count == 1
        assert job.progress_percent == 50

    def test_ordering_by_created_at_descending(self):
        question_set = QuestionSet.objects.create(name="TestSet")
        job1 = BackfillJob.objects.create(question_set=question_set)
        job2 = BackfillJob.objects.create(question_set=question_set)
        jobs = list(BackfillJob.objects.all())
        assert jobs[0] == job2
        assert jobs[1] == job1

    def test_cascade_delete_with_set(self):
        question_set = QuestionSet.objects.create(name="DeleteMe")
        BackfillJob.objects.create(question_set=question_set)
        assert BackfillJob.objects.count() == 1
        question_set.delete()
        assert BackfillJob.objects.count() == 0


# -- Signal behavior --


@pytest.mark.django_db
class TestQuestionSignals:
    """Question save side effects. Backfill scheduling does NOT happen on
    save anymore — that's the nightly launcher's job."""

    def test_creating_question_does_not_immediately_fire_backfill(self):
        question_set = QuestionSet.objects.create(name="Findings")
        Question.objects.create(question_set=question_set, label="PE present?")

        assert BackfillJob.objects.filter(question_set=question_set).count() == 0

    def test_creating_question_creates_default_answer_options(self):
        question_set = QuestionSet.objects.create(name="Findings")
        question = Question.objects.create(question_set=question_set, label="PE present?")

        assert question.options.count() == 3
        assert question.options.filter(is_unknown=True).count() == 1

    def test_question_save_bumps_last_edited_at(self):
        question_set = QuestionSet.objects.create(name="Findings")
        assert question_set.last_edited_at is None

        Question.objects.create(question_set=question_set, label="PE present?")
        question_set.refresh_from_db()
        assert question_set.last_edited_at is not None


# -- Nightly launcher --


@pytest.mark.django_db
class TestLabelsBackfillLauncher:
    """The nightly launcher dispatches one backfill per dirty set, respects
    the same lock that the cancel/retry views respect, and treats a set
    with no outstanding work as a no-op.
    """

    def _run_launcher(self):
        # The decorated launcher is a Procrastinate task wrapper; the original
        # callable is exposed on `.func`. Call that synchronously in tests.
        from radis.labels.tasks import labels_backfill_launcher

        labels_backfill_launcher.func(timestamp=0)

    @patch("radis.labels.tasks.enqueue_question_set_backfill")
    def test_dispatches_for_dirty_set(self, mock_task):
        mock_task.defer = lambda **kw: None
        question_set = QuestionSet.objects.create(name="Dirty")
        _make_question(question_set, "Q1")
        _make_report("doc-1")  # makes set "dirty" — has missing reports

        self._run_launcher()

        assert BackfillJob.objects.filter(
            question_set=question_set, status=BackfillJob.Status.PENDING
        ).count() == 1

    @patch("radis.labels.tasks.enqueue_question_set_backfill")
    def test_skips_set_with_no_outstanding_work(self, mock_task):
        mock_task.defer = lambda **kw: None
        question_set = QuestionSet.objects.create(name="Clean")
        _make_question(question_set, "Q1")
        report = _make_report("doc-1")
        _record_complete_runs(report, question_set)  # all modes complete

        self._run_launcher()

        assert BackfillJob.objects.filter(question_set=question_set).count() == 0

    @patch("radis.labels.tasks.enqueue_question_set_backfill")
    def test_skips_locked_set(self, mock_task):
        mock_task.defer = lambda **kw: None
        question_set = QuestionSet.objects.create(name="Locked")
        _make_question(question_set, "Q1")
        _make_report("doc-1")
        # Existing IN_PROGRESS backfill = lock — launcher must not stack.
        BackfillJob.objects.create(
            question_set=question_set, status=BackfillJob.Status.IN_PROGRESS
        )

        self._run_launcher()

        # Still just the one we pre-created.
        assert BackfillJob.objects.filter(question_set=question_set).count() == 1

    @patch("radis.labels.tasks.enqueue_question_set_backfill")
    def test_skips_inactive_set(self, mock_task):
        mock_task.defer = lambda **kw: None
        question_set = QuestionSet.objects.create(name="Inactive", is_active=False)
        _make_question(question_set, "Q1")
        _make_report("doc-1")

        self._run_launcher()

        assert BackfillJob.objects.filter(question_set=question_set).count() == 0

    @patch("radis.labels.tasks.enqueue_question_set_backfill")
    def test_dispatches_per_set(self, mock_task):
        mock_task.defer = lambda **kw: None
        set_a = QuestionSet.objects.create(name="Set A")
        set_b = QuestionSet.objects.create(name="Set B")
        _make_question(set_a, "QA")
        _make_question(set_b, "QB")
        _make_report("doc-1")

        self._run_launcher()

        assert BackfillJob.objects.filter(question_set=set_a).count() == 1
        assert BackfillJob.objects.filter(question_set=set_b).count() == 1


# -- QuestionSet.missing_reports tests --


@pytest.mark.django_db
class TestQuestionSetMissingReports:
    def test_empty_set_returns_no_missing_reports(self):
        question_set = QuestionSet.objects.create(name="EmptySet")
        _make_report("doc-1")
        assert list(question_set.missing_reports()) == []

    def test_unlabelled_report_is_missing(self):
        question_set = QuestionSet.objects.create(name="S")
        _make_question(question_set, "Q1")
        report = _make_report("doc-1")
        assert list(question_set.missing_reports()) == [report]

    def test_fully_labelled_report_is_not_missing(self):
        question_set = QuestionSet.objects.create(name="S")
        _make_question(question_set, "Q1")
        report = _make_report("doc-1")
        _record_complete_runs(report, question_set)
        assert list(question_set.missing_reports()) == []

    def test_report_without_run_is_missing_even_with_other_set_runs(self):
        set_a = QuestionSet.objects.create(name="SetA")
        _make_question(set_a, "QA")
        set_b = QuestionSet.objects.create(name="SetB")
        _make_question(set_b, "QB")
        report = _make_report("doc-1")
        _record_complete_runs(report, set_a)
        assert list(set_b.missing_reports()) == [report]

    def test_inactive_questions_do_not_count_toward_completion(self):
        question_set = QuestionSet.objects.create(name="S")
        _make_question(question_set, "Q1")
        Question.objects.create(
            question_set=question_set, label="QInactive", is_active=False
        )
        report = _make_report("doc-1")
        # Runs cover only the active question. The helper iterates active
        # questions for every required mode, so the inactive question is
        # naturally excluded — and missing_reports must agree.
        _record_complete_runs(report, question_set)
        assert list(question_set.missing_reports()) == []


# -- _maybe_finalize tests --


@pytest.mark.django_db
class TestMaybeFinalize:
    def _setup_in_progress(
        self,
        with_question: bool = True,
        status=BackfillJob.Status.IN_PROGRESS,
        total: int = 100,
    ) -> tuple[BackfillJob, QuestionSet]:
        question_set = QuestionSet.objects.create(name="S")
        if with_question:
            _make_question(question_set, "Q1")
        job = BackfillJob.objects.create(
            question_set=question_set,
            status=status,
            total_reports=total,
            processed_reports=0,
        )
        return job, question_set

    def test_noop_for_terminal_status(self):
        job, _ = self._setup_in_progress(
            with_question=False, status=BackfillJob.Status.SUCCESS
        )
        _maybe_finalize(job.id)
        job.refresh_from_db()
        assert job.status == BackfillJob.Status.SUCCESS
        assert job.ended_at is None

    def test_noop_when_missing_reports_remain(self):
        job, _ = self._setup_in_progress(with_question=True)
        _make_report("doc-1")
        _maybe_finalize(job.id)
        job.refresh_from_db()
        assert job.status == BackfillJob.Status.IN_PROGRESS
        assert job.ended_at is None

    def test_finalizes_to_success_when_no_missing_reports(self):
        job, question_set = self._setup_in_progress(with_question=True)
        report = _make_report("doc-1")
        _record_complete_runs(report, question_set)
        _maybe_finalize(job.id)
        job.refresh_from_db()
        assert job.status == BackfillJob.Status.SUCCESS
        assert job.ended_at is not None
        assert job.processed_reports == job.total_reports

    def test_does_not_finalize_canceled_job(self):
        job, _ = self._setup_in_progress(
            with_question=False, status=BackfillJob.Status.CANCELED
        )
        _maybe_finalize(job.id)
        job.refresh_from_db()
        assert job.status == BackfillJob.Status.CANCELED

    def test_handles_missing_job_gracefully(self):
        _maybe_finalize(99999)

    def test_concurrent_cancel_is_not_overwritten(self):
        """If a concurrent cancel flips status to CANCELED, finalize must no-op."""
        job, _ = self._setup_in_progress(with_question=False)

        original_get = BackfillJob.objects.get

        def get_then_cancel(*args, **kwargs):
            job_obj = original_get(*args, **kwargs)
            BackfillJob.objects.filter(id=job_obj.id).update(
                status=BackfillJob.Status.CANCELED
            )
            return job_obj

        with patch.object(BackfillJob.objects, "get", side_effect=get_then_cancel):
            _maybe_finalize(job.id)

        job.refresh_from_db()
        assert job.status == BackfillJob.Status.CANCELED


# -- Cancel view tests --


@pytest.mark.django_db
class TestBackfillCancelView:
    def _create_job(self, status=BackfillJob.Status.IN_PROGRESS) -> BackfillJob:
        question_set = QuestionSet.objects.create(name="TestSet")
        return BackfillJob.objects.create(
            question_set=question_set,
            status=status,
            total_reports=100,
        )

    def test_cancel_requires_login(self, client: Client):
        job = self._create_job()
        response = client.post(f"/labels/backfill/{job.pk}/cancel/")
        assert response.status_code == 302
        assert "/accounts/login/" in response["Location"]

    def test_cancel_sets_canceled_status(self, client: Client):
        user = UserFactory.create(is_active=True, is_staff=True)
        client.force_login(user)
        job = self._create_job(status=BackfillJob.Status.IN_PROGRESS)

        response = client.post(f"/labels/backfill/{job.pk}/cancel/")
        assert response.status_code == 302

        job.refresh_from_db()
        assert job.status == BackfillJob.Status.CANCELED
        assert job.ended_at is not None

    def test_cancel_pending_job(self, client: Client):
        user = UserFactory.create(is_active=True, is_staff=True)
        client.force_login(user)
        job = self._create_job(status=BackfillJob.Status.PENDING)

        response = client.post(f"/labels/backfill/{job.pk}/cancel/")
        assert response.status_code == 302

        job.refresh_from_db()
        assert job.status == BackfillJob.Status.CANCELED

    def test_cancel_snapshots_progress_at_cancel_time(self, client: Client):
        user = UserFactory.create(is_active=True, is_staff=True)
        client.force_login(user)

        question_set = QuestionSet.objects.create(name="ProgressSet")
        _make_question(question_set, "Q1")
        labelled = _make_report("doc-labelled")
        _make_report("doc-unlabelled")
        _record_complete_runs(labelled, question_set)

        job = BackfillJob.objects.create(
            question_set=question_set,
            status=BackfillJob.Status.IN_PROGRESS,
            total_reports=2,
            processed_reports=0,
        )

        client.post(f"/labels/backfill/{job.pk}/cancel/")
        job.refresh_from_db()

        assert job.processed_reports == 1

    def test_cancel_rejected_for_non_staff(self, client: Client):
        user = UserFactory.create(is_active=True, is_staff=False)
        client.force_login(user)
        job = self._create_job(status=BackfillJob.Status.IN_PROGRESS)

        response = client.post(f"/labels/backfill/{job.pk}/cancel/")
        assert response.status_code == 403

        job.refresh_from_db()
        assert job.status == BackfillJob.Status.IN_PROGRESS

    def test_cancel_already_completed_returns_400(self, client: Client):
        user = UserFactory.create(is_active=True, is_staff=True)
        client.force_login(user)
        job = self._create_job(status=BackfillJob.Status.SUCCESS)

        response = client.post(f"/labels/backfill/{job.pk}/cancel/")
        assert response.status_code == 400

    def test_cancel_nonexistent_job_returns_404(self, client: Client):
        user = UserFactory.create(is_active=True, is_staff=True)
        client.force_login(user)

        response = client.post("/labels/backfill/99999/cancel/")
        assert response.status_code == 404

    def test_cancel_redirects_to_set_detail(self, client: Client):
        user = UserFactory.create(is_active=True, is_staff=True)
        client.force_login(user)
        job = self._create_job()

        response = client.post(f"/labels/backfill/{job.pk}/cancel/")
        assert response.status_code == 302
        assert f"/labels/{job.question_set_id}/" in response["Location"]

    def test_get_not_allowed(self, client: Client):
        user = UserFactory.create(is_active=True, is_staff=True)
        client.force_login(user)
        job = self._create_job()

        response = client.get(f"/labels/backfill/{job.pk}/cancel/")
        assert response.status_code == 405


# -- Retry view tests --


@pytest.mark.django_db
class TestBackfillRetryView:
    def _create_job(self, status=BackfillJob.Status.FAILURE) -> BackfillJob:
        question_set = QuestionSet.objects.create(name="TestSet")
        return BackfillJob.objects.create(
            question_set=question_set,
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
        assert job.status == BackfillJob.Status.FAILURE

    def test_retry_resets_failed_job_to_pending(self, client: Client):
        user = UserFactory.create(is_active=True, is_staff=True)
        client.force_login(user)
        job = self._create_job(status=BackfillJob.Status.FAILURE)

        with patch("radis.labels.views.enqueue_question_set_backfill") as mock_task:
            mock_task.defer = lambda **kw: None
            response = client.post(f"/labels/backfill/{job.pk}/retry/")

        assert response.status_code == 302
        job.refresh_from_db()
        assert job.status == BackfillJob.Status.PENDING
        assert job.started_at is None
        assert job.ended_at is None

    def test_retry_canceled_job_to_pending(self, client: Client):
        user = UserFactory.create(is_active=True, is_staff=True)
        client.force_login(user)
        job = self._create_job(status=BackfillJob.Status.CANCELED)

        with patch("radis.labels.views.enqueue_question_set_backfill") as mock_task:
            mock_task.defer = lambda **kw: None
            client.post(f"/labels/backfill/{job.pk}/retry/")

        job.refresh_from_db()
        assert job.status == BackfillJob.Status.PENDING

    def test_retry_in_progress_job_returns_400(self, client: Client):
        user = UserFactory.create(is_active=True, is_staff=True)
        client.force_login(user)
        job = self._create_job(status=BackfillJob.Status.IN_PROGRESS)

        response = client.post(f"/labels/backfill/{job.pk}/retry/")
        assert response.status_code == 400

        job.refresh_from_db()
        assert job.status == BackfillJob.Status.IN_PROGRESS

    def test_retry_skipped_when_other_active_backfill_exists(self, client: Client):
        user = UserFactory.create(is_active=True, is_staff=True)
        client.force_login(user)

        question_set = QuestionSet.objects.create(name="TestSet")
        failed_job = BackfillJob.objects.create(
            question_set=question_set,
            status=BackfillJob.Status.FAILURE,
            total_reports=100,
        )
        BackfillJob.objects.create(
            question_set=question_set,
            status=BackfillJob.Status.IN_PROGRESS,
            total_reports=100,
        )

        with patch("radis.labels.views.enqueue_question_set_backfill") as mock_task:
            response = client.post(f"/labels/backfill/{failed_job.pk}/retry/")

        assert response.status_code == 302
        failed_job.refresh_from_db()
        assert failed_job.status == BackfillJob.Status.FAILURE
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
class TestQuestionSetDetailViewBackfill:
    def test_detail_view_includes_backfill_job(self, client: Client):
        user = UserFactory.create(is_active=True)
        client.force_login(user)

        question_set = QuestionSet.objects.create(name="Findings")
        job = BackfillJob.objects.create(
            question_set=question_set,
            status=BackfillJob.Status.IN_PROGRESS,
            total_reports=500,
            processed_reports=200,
        )

        response = client.get(f"/labels/{question_set.pk}/")
        assert response.status_code == 200
        assert response.context["backfill_job"] == job

    def test_detail_view_returns_most_recent_backfill(self, client: Client):
        user = UserFactory.create(is_active=True)
        client.force_login(user)

        question_set = QuestionSet.objects.create(name="Findings")
        BackfillJob.objects.create(
            question_set=question_set,
            status=BackfillJob.Status.SUCCESS,
        )
        job2 = BackfillJob.objects.create(
            question_set=question_set,
            status=BackfillJob.Status.IN_PROGRESS,
        )

        response = client.get(f"/labels/{question_set.pk}/")
        assert response.context["backfill_job"] == job2

    def test_detail_view_no_backfill_job(self, client: Client):
        user = UserFactory.create(is_active=True)
        client.force_login(user)

        question_set = QuestionSet.objects.create(name="Findings")
        response = client.get(f"/labels/{question_set.pk}/")
        assert response.context["backfill_job"] is None


# -- Management command tests --


@pytest.mark.django_db
class TestLabelsBackfillCommand:
    @patch("radis.labels.management.commands.labels_backfill.process_question_set_batch")
    def test_command_creates_backfill_job(self, mock_task):
        mock_task.defer = lambda **kw: None
        from django.core.management import call_command

        from radis.reports.models import Language, Report

        question_set = QuestionSet.objects.create(name="Findings")
        lang = Language.objects.create(code="en")
        Report.objects.create(
            document_id="doc-1",
            body="Test report body",
            patient_birth_date="2000-01-01",
            patient_sex="M",
            study_datetime="2024-01-15T10:00:00Z",
            language=lang,
        )

        call_command("labels_backfill", question_set=str(question_set.id))

        job = BackfillJob.objects.get(question_set=question_set)
        assert job.status == BackfillJob.Status.IN_PROGRESS
        assert job.total_reports == 1
        assert job.started_at is not None


# -- Templatetag tests --


class TestBackfillStatusCssFilter:
    def test_pending(self):
        from radis.labels.templatetags.labels_extras import backfill_status_css

        assert backfill_status_css(BackfillJob.Status.PENDING) == "text-secondary"

    def test_in_progress(self):
        from radis.labels.templatetags.labels_extras import backfill_status_css

        assert backfill_status_css(BackfillJob.Status.IN_PROGRESS) == "text-info"

    def test_success(self):
        from radis.labels.templatetags.labels_extras import backfill_status_css

        assert backfill_status_css(BackfillJob.Status.SUCCESS) == "text-success"

    def test_failure(self):
        from radis.labels.templatetags.labels_extras import backfill_status_css

        assert backfill_status_css(BackfillJob.Status.FAILURE) == "text-danger"

    def test_canceled(self):
        from radis.labels.templatetags.labels_extras import backfill_status_css

        assert backfill_status_css(BackfillJob.Status.CANCELED) == "text-muted"

    def test_unknown_status(self):
        from radis.labels.templatetags.labels_extras import backfill_status_css

        assert backfill_status_css("XX") == ""


# -- Lock guard test --


@pytest.mark.django_db
class TestQuestionSetLock:
    def test_is_locked_when_active_backfill_exists(self):
        question_set = QuestionSet.objects.create(name="LockedSet")
        BackfillJob.objects.create(
            question_set=question_set, status=BackfillJob.Status.IN_PROGRESS
        )
        assert question_set.is_locked is True

    def test_is_not_locked_for_terminal_backfill(self):
        question_set = QuestionSet.objects.create(name="DoneSet")
        BackfillJob.objects.create(
            question_set=question_set, status=BackfillJob.Status.SUCCESS
        )
        assert question_set.is_locked is False

    def test_question_update_view_blocks_when_locked(self, client: Client):
        user = UserFactory.create(is_active=True, is_staff=True)
        client.force_login(user)
        question_set = QuestionSet.objects.create(name="LockedSet")
        question = _make_question(question_set, "Q1")
        BackfillJob.objects.create(
            question_set=question_set, status=BackfillJob.Status.IN_PROGRESS
        )

        response = client.post(
            f"/labels/{question_set.pk}/questions/{question.pk}/update/",
            {"label": "Q1 renamed", "question": "", "is_active": True, "order": 0},
        )
        # Locked => redirect back to detail with a warning, no save.
        assert response.status_code == 302
        question.refresh_from_db()
        assert question.label == "Q1"
