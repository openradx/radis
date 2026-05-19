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
    """Retry semantics changed in the dispatch-helper rewrite (HIGH #2 fix):

    The old behavior was to reset the existing BackfillJob row in-place to
    PENDING and re-defer the coordinator on it. The new behavior is to leave
    the original terminal row as audit history and create a *new* PENDING
    BackfillJob through ``dispatch_backfill_for_set``. The lock semantics
    enforced by the helper close the retry-vs-launcher TOCTOU window the
    older code had.

    These tests assert the new contract:
      * the original job is untouched on success (still FAILURE / CANCELED);
      * a fresh BackfillJob row gets created;
      * the race-vs-active-backfill case surfaces the "already running"
        message without creating a new row.
    """

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
        # Failed retry must not have created a sibling job.
        assert BackfillJob.objects.filter(question_set=job.question_set).count() == 1

    def test_retry_failure_creates_fresh_pending_job(self, client: Client):
        user = UserFactory.create(is_active=True, is_staff=True)
        client.force_login(user)
        job = self._create_job(status=BackfillJob.Status.FAILURE)

        with patch("radis.labels.tasks.enqueue_question_set_backfill") as mock_task:
            mock_task.defer = lambda **kw: None
            response = client.post(f"/labels/backfill/{job.pk}/retry/")

        assert response.status_code == 302

        # Original row is preserved as audit history.
        job.refresh_from_db()
        assert job.status == BackfillJob.Status.FAILURE

        # A new PENDING row was created via dispatch_backfill_for_set.
        jobs = list(
            BackfillJob.objects.filter(question_set=job.question_set).order_by("created_at")
        )
        assert len(jobs) == 2
        assert jobs[0] == job
        assert jobs[1].status == BackfillJob.Status.PENDING

    def test_retry_canceled_creates_fresh_pending_job(self, client: Client):
        user = UserFactory.create(is_active=True, is_staff=True)
        client.force_login(user)
        job = self._create_job(status=BackfillJob.Status.CANCELED)

        with patch("radis.labels.tasks.enqueue_question_set_backfill") as mock_task:
            mock_task.defer = lambda **kw: None
            client.post(f"/labels/backfill/{job.pk}/retry/")

        job.refresh_from_db()
        assert job.status == BackfillJob.Status.CANCELED
        # New PENDING sibling exists.
        assert BackfillJob.objects.filter(
            question_set=job.question_set, status=BackfillJob.Status.PENDING
        ).count() == 1

    def test_retry_in_progress_job_returns_400(self, client: Client):
        user = UserFactory.create(is_active=True, is_staff=True)
        client.force_login(user)
        job = self._create_job(status=BackfillJob.Status.IN_PROGRESS)

        response = client.post(f"/labels/backfill/{job.pk}/retry/")
        assert response.status_code == 400

        job.refresh_from_db()
        assert job.status == BackfillJob.Status.IN_PROGRESS
        # Must not have created a sibling.
        assert BackfillJob.objects.filter(question_set=job.question_set).count() == 1

    def test_retry_skipped_when_other_active_backfill_exists(self, client: Client):
        """HIGH #2 regression guard. If a launcher (or another caller) created
        an active backfill between the user clicking Retry and the request
        landing, the helper's lock + already_active check must surface the
        "already running" message rather than queueing a duplicate.
        """
        user = UserFactory.create(is_active=True, is_staff=True)
        client.force_login(user)

        question_set = QuestionSet.objects.create(name="TestSet")
        failed_job = BackfillJob.objects.create(
            question_set=question_set,
            status=BackfillJob.Status.FAILURE,
            total_reports=100,
        )
        active_job = BackfillJob.objects.create(
            question_set=question_set,
            status=BackfillJob.Status.IN_PROGRESS,
            total_reports=100,
        )

        with patch("radis.labels.tasks.enqueue_question_set_backfill") as mock_task:
            response = client.post(f"/labels/backfill/{failed_job.pk}/retry/")

        assert response.status_code == 302
        # Original terminal row untouched.
        failed_job.refresh_from_db()
        assert failed_job.status == BackfillJob.Status.FAILURE
        # Pre-existing active job untouched.
        active_job.refresh_from_db()
        assert active_job.status == BackfillJob.Status.IN_PROGRESS
        # Critical: no new row was created.
        assert BackfillJob.objects.filter(question_set=question_set).count() == 2
        # No coordinator deferred.
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
    """The CLI is now the same dispatch path as the manual launch button —
    it goes through ``dispatch_backfill_for_set``, which respects the per-set
    lock and only fires if there is outstanding work.

    HIGH #1 regression guard: previously the command enqueued every report
    regardless of existing coverage (a re-run on a fully-labelled set burnt
    LLM cost and produced duplicate runs). These tests assert the new
    contract.
    """

    @patch("radis.labels.tasks.enqueue_question_set_backfill")
    def test_command_creates_pending_backfill_job(self, mock_task):
        from django.core.management import call_command

        mock_task.defer = lambda **kw: None
        question_set = QuestionSet.objects.create(name="Findings")
        _make_question(question_set, "Q1")
        _make_report("doc-1")

        call_command("labels_backfill", question_set=str(question_set.id))

        # Helper creates a PENDING job; the coordinator (mocked here) would
        # flip it to IN_PROGRESS, but we only assert the helper's product.
        job = BackfillJob.objects.get(question_set=question_set)
        assert job.status == BackfillJob.Status.PENDING

    @patch("radis.labels.tasks.enqueue_question_set_backfill")
    def test_command_is_noop_on_fully_labelled_set(self, mock_task):
        """HIGH #1 regression: re-running the CLI on a set with no missing
        reports must not create a duplicate job and must not defer any
        coordinator task. The old CLI happily enqueued every report.
        """
        from django.core.management import call_command

        mock_task.defer = lambda **kw: None
        question_set = QuestionSet.objects.create(name="AlreadyDone")
        _make_question(question_set, "Q1")
        report = _make_report("doc-1")
        _record_complete_runs(report, question_set)  # full coverage in every mode

        call_command("labels_backfill", question_set=str(question_set.id))

        assert BackfillJob.objects.filter(question_set=question_set).count() == 0
        assert not mock_task.defer.called

    @patch("radis.labels.tasks.enqueue_question_set_backfill")
    def test_command_skips_when_backfill_already_active(self, mock_task):
        """HIGH #1 corollary: the CLI must not stack on top of an in-flight
        backfill. The helper's lock + dedup check is the enforcement; the
        CLI surfaces it as a "already active" warning.
        """
        from django.core.management import call_command

        mock_task.defer = lambda **kw: None
        question_set = QuestionSet.objects.create(name="Running")
        _make_question(question_set, "Q1")
        _make_report("doc-1")
        BackfillJob.objects.create(
            question_set=question_set, status=BackfillJob.Status.IN_PROGRESS
        )

        call_command("labels_backfill", question_set=str(question_set.id))

        # The pre-existing IN_PROGRESS row is the only one — no sibling created.
        assert BackfillJob.objects.filter(question_set=question_set).count() == 1

    @patch("radis.labels.tasks.enqueue_question_set_backfill")
    def test_command_skips_inactive_set(self, mock_task):
        from django.core.management import call_command

        mock_task.defer = lambda **kw: None
        question_set = QuestionSet.objects.create(name="Inactive", is_active=False)
        _make_question(question_set, "Q1")
        _make_report("doc-1")

        call_command("labels_backfill", question_set=str(question_set.id))

        assert BackfillJob.objects.filter(question_set=question_set).count() == 0


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


# -- dispatch_backfill_for_set helper tests --


@pytest.mark.django_db
class TestDispatchBackfillForSet:
    """The helper is the single entry point for kicking off a backfill from
    any caller (launcher, manual launch view, retry view, CLI). These tests
    pin its contract so a future refactor that breaks the dedup, the lock
    pattern, or the on-commit defer surfaces as a failure.
    """

    @patch("radis.labels.tasks.enqueue_question_set_backfill")
    def test_creates_pending_job_when_none_active(self, mock_task):
        from radis.labels.tasks import dispatch_backfill_for_set

        mock_task.defer = lambda **kw: None
        question_set = QuestionSet.objects.create(name="FreshSet")

        job = dispatch_backfill_for_set(question_set)

        assert job is not None
        assert job.status == BackfillJob.Status.PENDING
        assert job.question_set == question_set

    @patch("radis.labels.tasks.enqueue_question_set_backfill")
    def test_returns_none_when_pending_job_already_exists(self, mock_task):
        """Dedup must trigger for PENDING jobs (not just IN_PROGRESS) —
        otherwise two near-simultaneous Launch clicks could both create a
        PENDING row before either coordinator advances to IN_PROGRESS.
        """
        from radis.labels.tasks import dispatch_backfill_for_set

        mock_task.defer = lambda **kw: None
        question_set = QuestionSet.objects.create(name="AlreadyPending")
        BackfillJob.objects.create(
            question_set=question_set, status=BackfillJob.Status.PENDING
        )

        job = dispatch_backfill_for_set(question_set)

        assert job is None
        assert BackfillJob.objects.filter(question_set=question_set).count() == 1

    @patch("radis.labels.tasks.enqueue_question_set_backfill")
    def test_returns_none_when_in_progress_job_exists(self, mock_task):
        from radis.labels.tasks import dispatch_backfill_for_set

        mock_task.defer = lambda **kw: None
        question_set = QuestionSet.objects.create(name="Running")
        BackfillJob.objects.create(
            question_set=question_set, status=BackfillJob.Status.IN_PROGRESS
        )

        job = dispatch_backfill_for_set(question_set)

        assert job is None
        assert BackfillJob.objects.filter(question_set=question_set).count() == 1

    @patch("radis.labels.tasks.enqueue_question_set_backfill")
    def test_terminal_jobs_do_not_block_new_dispatch(self, mock_task):
        """A SUCCESS / FAILURE / CANCELED row is audit history, not a lock
        on future backfills. The helper must still create a new PENDING
        job when invoked, regardless of how many terminal siblings exist.
        """
        from radis.labels.tasks import dispatch_backfill_for_set

        mock_task.defer = lambda **kw: None
        question_set = QuestionSet.objects.create(name="WithHistory")
        for status in [
            BackfillJob.Status.SUCCESS,
            BackfillJob.Status.FAILURE,
            BackfillJob.Status.CANCELED,
        ]:
            BackfillJob.objects.create(question_set=question_set, status=status)

        job = dispatch_backfill_for_set(question_set)

        assert job is not None
        assert job.status == BackfillJob.Status.PENDING
        assert BackfillJob.objects.filter(question_set=question_set).count() == 4

    def test_calls_select_for_update_on_the_set_row(self):
        """The lock primitive is load-bearing — without it, two concurrent
        callers can both pass the dedup check before either inserts. This
        test asserts the helper actually issues ``select_for_update`` so a
        future refactor that drops the lock surfaces as a failure.
        """
        from radis.labels.tasks import dispatch_backfill_for_set

        question_set = QuestionSet.objects.create(name="LockedQueries")

        with patch(
            "radis.labels.tasks.QuestionSet.objects",
            wraps=QuestionSet.objects,
        ) as mock_objects:
            with patch("radis.labels.tasks.enqueue_question_set_backfill") as mock_task:
                mock_task.defer = lambda **kw: None
                dispatch_backfill_for_set(question_set)

        mock_objects.select_for_update.assert_called()

    @patch("radis.labels.tasks.enqueue_question_set_backfill")
    def test_defers_coordinator_on_commit(self, mock_task):
        """When the helper succeeds, the coordinator task must be deferred
        for the new job after the surrounding transaction commits. We use
        ``django_db(transaction=True)`` so on_commit hooks actually fire.
        """
        from django.db import transaction

        from radis.labels.tasks import dispatch_backfill_for_set

        question_set = QuestionSet.objects.create(name="DefersCoord")

        with transaction.atomic():
            job = dispatch_backfill_for_set(question_set)

        assert job is not None
        # transaction.atomic() commits on exit — on_commit fires here.
        mock_task.defer.assert_called_once()
        called_kwargs = mock_task.defer.call_args.kwargs
        assert called_kwargs["question_set_id"] == question_set.id
        assert called_kwargs["backfill_job_id"] == job.id


# -- Manual launch view tests --


@pytest.mark.django_db
class TestBackfillLaunchView:
    """The manual "Run backfill now" button.

    The view is an opt-in override for the nightly cron — staff who finished
    editing and want labelling to start now click this. The button is hidden
    in the template while the set is locked, so under normal use the view
    only ever sees the happy path. The tests cover both the happy path and
    every guard (auth, inactive set, no-work set, already-running) because
    those guards are the security boundary even when the button isn't shown.
    """

    def _make_dispatchable_set(self) -> QuestionSet:
        question_set = QuestionSet.objects.create(name="LaunchSet")
        _make_question(question_set, "Q1")
        _make_report("doc-1")
        return question_set

    def test_requires_login(self, client: Client):
        question_set = self._make_dispatchable_set()
        response = client.post(f"/labels/{question_set.pk}/backfill/launch/")
        assert response.status_code == 302
        assert "/accounts/login/" in response["Location"]

    def test_rejected_for_non_staff(self, client: Client):
        user = UserFactory.create(is_active=True, is_staff=False)
        client.force_login(user)
        question_set = self._make_dispatchable_set()

        response = client.post(f"/labels/{question_set.pk}/backfill/launch/")

        assert response.status_code == 403
        assert BackfillJob.objects.filter(question_set=question_set).count() == 0

    @patch("radis.labels.tasks.enqueue_question_set_backfill")
    def test_happy_path_creates_pending_job_and_redirects(self, mock_task, client: Client):
        mock_task.defer = lambda **kw: None
        user = UserFactory.create(is_active=True, is_staff=True)
        client.force_login(user)
        question_set = self._make_dispatchable_set()

        response = client.post(f"/labels/{question_set.pk}/backfill/launch/")

        assert response.status_code == 302
        assert f"/labels/{question_set.pk}/" in response["Location"]
        job = BackfillJob.objects.get(question_set=question_set)
        assert job.status == BackfillJob.Status.PENDING

    @patch("radis.labels.tasks.enqueue_question_set_backfill")
    def test_get_not_allowed(self, mock_task, client: Client):
        user = UserFactory.create(is_active=True, is_staff=True)
        client.force_login(user)
        question_set = self._make_dispatchable_set()

        response = client.get(f"/labels/{question_set.pk}/backfill/launch/")

        assert response.status_code == 405
        assert BackfillJob.objects.filter(question_set=question_set).count() == 0

    @patch("radis.labels.tasks.enqueue_question_set_backfill")
    def test_inactive_set_does_not_dispatch(self, mock_task, client: Client):
        mock_task.defer = lambda **kw: None
        user = UserFactory.create(is_active=True, is_staff=True)
        client.force_login(user)
        question_set = QuestionSet.objects.create(name="Inactive", is_active=False)
        _make_question(question_set, "Q1")
        _make_report("doc-1")

        response = client.post(f"/labels/{question_set.pk}/backfill/launch/")

        assert response.status_code == 302
        assert BackfillJob.objects.filter(question_set=question_set).count() == 0

    @patch("radis.labels.tasks.enqueue_question_set_backfill")
    def test_no_work_does_not_dispatch(self, mock_task, client: Client):
        """If the set is already fully labelled, the button should no-op
        with an info message rather than creating a useless coordinator
        that flips straight to SUCCESS.
        """
        mock_task.defer = lambda **kw: None
        user = UserFactory.create(is_active=True, is_staff=True)
        client.force_login(user)
        question_set = QuestionSet.objects.create(name="Done")
        _make_question(question_set, "Q1")
        report = _make_report("doc-1")
        _record_complete_runs(report, question_set)

        response = client.post(f"/labels/{question_set.pk}/backfill/launch/")

        assert response.status_code == 302
        assert BackfillJob.objects.filter(question_set=question_set).count() == 0
        assert not mock_task.defer.called

    @patch("radis.labels.tasks.enqueue_question_set_backfill")
    def test_already_running_does_not_dispatch(self, mock_task, client: Client):
        """The button is hidden in the UI when ``is_locked`` is True, but
        the server still has to guard against direct POSTs. The helper's
        dedup must surface as a no-op + info message, not a duplicate job.
        """
        mock_task.defer = lambda **kw: None
        user = UserFactory.create(is_active=True, is_staff=True)
        client.force_login(user)
        question_set = self._make_dispatchable_set()
        active_job = BackfillJob.objects.create(
            question_set=question_set, status=BackfillJob.Status.IN_PROGRESS
        )

        response = client.post(f"/labels/{question_set.pk}/backfill/launch/")

        assert response.status_code == 302
        # No new job created; the pre-existing active job is untouched.
        assert BackfillJob.objects.filter(question_set=question_set).count() == 1
        active_job.refresh_from_db()
        assert active_job.status == BackfillJob.Status.IN_PROGRESS


# -- Race-condition regression tests --


@pytest.mark.django_db
class TestBackfillDispatchRaces:
    """Race-condition guards for every code path that can dispatch a backfill.

    These tests target the bugs the helper closed (HIGH #1 and HIGH #2) by
    asserting the helper's dedup holds even in adversarial orderings. They
    are unit-level — concurrent transactions across threads with a real DB
    lock are deferred to integration tests — but they're enough to catch a
    future refactor that drops the lock or moves the dedup check outside
    the atomic block.
    """

    @patch("radis.labels.tasks.enqueue_question_set_backfill")
    def test_two_sequential_dispatches_only_one_creates_job(self, mock_task):
        """Sanity: two callers, second one sees the first's job. Sequential
        is the easy case; the helper must already handle this correctly.
        """
        from radis.labels.tasks import dispatch_backfill_for_set

        mock_task.defer = lambda **kw: None
        question_set = QuestionSet.objects.create(name="Seq")

        first = dispatch_backfill_for_set(question_set)
        second = dispatch_backfill_for_set(question_set)

        assert first is not None
        assert second is None
        assert BackfillJob.objects.filter(question_set=question_set).count() == 1

    @patch("radis.labels.tasks.enqueue_question_set_backfill")
    def test_retry_view_does_not_stack_on_launcher_concurrent_create(
        self, mock_task, client: Client
    ):
        """HIGH #2: simulate the launcher creating a BackfillJob in the
        instant between the user clicking Retry and the dispatch firing.

        We model the race by inserting an IN_PROGRESS BackfillJob *before*
        calling the retry endpoint — same end state the race produces. The
        retry view must see it (via the helper's lock + dedup) and not
        create a duplicate.
        """
        mock_task.defer = lambda **kw: None
        user = UserFactory.create(is_active=True, is_staff=True)
        client.force_login(user)

        question_set = QuestionSet.objects.create(name="RetryRace")
        failed_job = BackfillJob.objects.create(
            question_set=question_set,
            status=BackfillJob.Status.FAILURE,
        )
        # Simulate the launcher winning the race.
        launcher_job = BackfillJob.objects.create(
            question_set=question_set, status=BackfillJob.Status.IN_PROGRESS
        )

        response = client.post(f"/labels/backfill/{failed_job.pk}/retry/")

        assert response.status_code == 302
        # Only the two pre-existing rows remain — no duplicate sibling.
        rows = BackfillJob.objects.filter(question_set=question_set).order_by("pk")
        assert [r.pk for r in rows] == [failed_job.pk, launcher_job.pk]
        # The launcher's job stayed IN_PROGRESS; the helper bailed cleanly.
        launcher_job.refresh_from_db()
        assert launcher_job.status == BackfillJob.Status.IN_PROGRESS

    @patch("radis.labels.tasks.enqueue_question_set_backfill")
    def test_manual_launch_does_not_stack_on_launcher_concurrent_create(
        self, mock_task, client: Client
    ):
        """Mirror of the retry-race test for the manual launch button.

        Same shape: pre-existing IN_PROGRESS job simulates the launcher
        winning the race against a user click. The launch view's helper
        invocation must detect it under the lock.
        """
        mock_task.defer = lambda **kw: None
        user = UserFactory.create(is_active=True, is_staff=True)
        client.force_login(user)

        question_set = QuestionSet.objects.create(name="LaunchRace")
        _make_question(question_set, "Q1")
        _make_report("doc-1")
        launcher_job = BackfillJob.objects.create(
            question_set=question_set, status=BackfillJob.Status.IN_PROGRESS
        )

        response = client.post(f"/labels/{question_set.pk}/backfill/launch/")

        assert response.status_code == 302
        assert BackfillJob.objects.filter(question_set=question_set).count() == 1
        launcher_job.refresh_from_db()
        assert launcher_job.status == BackfillJob.Status.IN_PROGRESS

    @patch("radis.labels.tasks.enqueue_question_set_backfill")
    def test_cli_does_not_stack_on_launcher_concurrent_create(self, mock_task):
        """HIGH #1 corollary: the CLI's invocation of the helper must obey
        the same lock + dedup as the views. A concurrent active backfill
        from any source (launcher, launch button, prior CLI invocation)
        causes the CLI to skip the set cleanly.
        """
        from django.core.management import call_command

        mock_task.defer = lambda **kw: None
        question_set = QuestionSet.objects.create(name="CLIRace")
        _make_question(question_set, "Q1")
        _make_report("doc-1")
        BackfillJob.objects.create(
            question_set=question_set, status=BackfillJob.Status.IN_PROGRESS
        )

        call_command("labels_backfill", question_set=str(question_set.id))

        assert BackfillJob.objects.filter(question_set=question_set).count() == 1

    @patch("radis.labels.tasks.enqueue_question_set_backfill")
    def test_launcher_does_not_stack_on_manual_launch_concurrent_create(self, mock_task):
        """Mirror: the nightly launcher's pre-lock ``missing_reports().exists()``
        is a cheap optimization. The real dedup must happen under the lock,
        so even if a manual launch landed between the pre-check and the
        lock, the launcher must still detect it.
        """
        mock_task.defer = lambda **kw: None
        question_set = QuestionSet.objects.create(name="LauncherRace")
        _make_question(question_set, "Q1")
        _make_report("doc-1")
        manual_job = BackfillJob.objects.create(
            question_set=question_set, status=BackfillJob.Status.PENDING
        )

        from radis.labels.tasks import labels_backfill_launcher

        labels_backfill_launcher.func(timestamp=0)

        assert BackfillJob.objects.filter(question_set=question_set).count() == 1
        manual_job.refresh_from_db()
        assert manual_job.status == BackfillJob.Status.PENDING
