import pytest
from django.db import IntegrityError, transaction

from radis.labels.factories import LabelingJobFactory
from radis.labels.models import LabelingJob


@pytest.mark.django_db
def test_only_one_active_labeling_job_allowed():
    LabelingJobFactory.create(status=LabelingJob.Status.PENDING)
    with pytest.raises(IntegrityError):
        with transaction.atomic():
            LabelingJobFactory.create(status=LabelingJob.Status.IN_PROGRESS)


@pytest.mark.django_db
def test_finished_jobs_do_not_count_as_active():
    LabelingJobFactory.create(status=LabelingJob.Status.SUCCESS)
    LabelingJobFactory.create(status=LabelingJob.Status.FAILURE)
    LabelingJobFactory.create(status=LabelingJob.Status.PENDING)
    assert LabelingJob.objects.filter(status=LabelingJob.Status.PENDING).count() == 1


@pytest.mark.django_db
def test_scan_job_owner_may_be_null():
    job = LabelingJob.objects.create(
        trigger=LabelingJob.Trigger.SCAN, status=LabelingJob.Status.PENDING, owner=None
    )
    assert job.owner is None


@pytest.mark.django_db
def test_finished_mail_is_a_noop_for_labeling_jobs():
    # Labeling jobs never send completion mail; neither an owner-less scan job nor an owned
    # manual job with send_finished_mail=True may crash (the base implementation would raise
    # ValueError on the None finished_mail_template).
    from adit_radis_shared.accounts.factories import UserFactory

    scan_job = LabelingJob.objects.create(
        trigger=LabelingJob.Trigger.SCAN,
        status=LabelingJob.Status.SUCCESS,
        owner=None,
        send_finished_mail=True,
    )
    scan_job._send_job_finished_mail()  # no-op, no AttributeError

    owned_job = LabelingJob.objects.create(
        trigger=LabelingJob.Trigger.MANUAL,
        status=LabelingJob.Status.SUCCESS,
        owner=UserFactory.create(),
        send_finished_mail=True,
    )
    owned_job._send_job_finished_mail()  # no-op, no crash even with an owner present


@pytest.mark.django_db
def test_manual_job_creates_tasks_for_needs_work_reports(monkeypatch):
    from radis.labels import tasks
    from radis.labels.factories import LabelFactory, LabelGroupFactory
    from radis.labels.models import LabelingTask
    from radis.reports.factories import ReportFactory

    group = LabelGroupFactory.create()
    LabelFactory.create(group=group)
    ReportFactory.create()
    ReportFactory.create()
    job = LabelingJobFactory.create(
        trigger=LabelingJob.Trigger.MANUAL, status=LabelingJob.Status.PENDING
    )

    monkeypatch.setattr(LabelingTask, "delay", lambda self: None)
    tasks.process_labeling_job(job.pk)

    job.refresh_from_db()
    assert job.tasks.exists()
    report_ids = set()
    for task in job.tasks.all():
        report_ids.update(task.reports.values_list("pk", flat=True))
    assert len(report_ids) == 2


@pytest.mark.django_db
def test_scan_job_only_includes_reports_after_scan_from(monkeypatch):
    from datetime import timedelta

    from django.utils import timezone

    from radis.labels import tasks
    from radis.labels.factories import LabelFactory, LabelGroupFactory
    from radis.labels.models import LabelingTask
    from radis.reports.factories import ReportFactory
    from radis.reports.models import Report

    LabelFactory.create(group=LabelGroupFactory.create())
    old = ReportFactory.create()
    Report.objects.filter(pk=old.pk).update(created_at=timezone.now() - timedelta(days=10))
    cutoff = timezone.now() - timedelta(days=1)
    new = ReportFactory.create()

    job = LabelingJobFactory.create(
        trigger=LabelingJob.Trigger.SCAN, scan_from=cutoff, status=LabelingJob.Status.PENDING
    )
    monkeypatch.setattr(LabelingTask, "delay", lambda self: None)
    tasks.process_labeling_job(job.pk)

    job.refresh_from_db()
    included = set()
    for task in job.tasks.all():
        included.update(task.reports.values_list("pk", flat=True))
    assert new.pk in included
    assert old.pk not in included


@pytest.mark.django_db
def test_process_job_is_idempotent_on_retry(monkeypatch):
    from radis.labels import tasks
    from radis.labels.factories import LabelFactory, LabelGroupFactory
    from radis.labels.models import LabelingTask
    from radis.reports.factories import ReportFactory

    LabelFactory.create(group=LabelGroupFactory.create())
    ReportFactory.create()
    job = LabelingJobFactory.create(
        trigger=LabelingJob.Trigger.MANUAL, status=LabelingJob.Status.PENDING
    )
    monkeypatch.setattr(LabelingTask, "delay", lambda self: None)

    tasks.process_labeling_job(job.pk)
    first_count = job.tasks.count()
    tasks.process_labeling_job(job.pk)  # simulate Procrastinate retry
    assert job.tasks.count() == first_count


@pytest.mark.django_db
def test_process_job_bails_on_in_progress_job_without_wiping_tasks(monkeypatch):
    # A spurious re-fire on an already-running job must NOT delete its in-flight tasks.
    from radis.core.models import AnalysisTask
    from radis.labels import tasks
    from radis.labels.factories import LabelingTaskFactory

    job = LabelingJobFactory.create(status=LabelingJob.Status.IN_PROGRESS)
    LabelingTaskFactory.create(job=job, status=AnalysisTask.Status.IN_PROGRESS)

    tasks.process_labeling_job(job.pk)

    assert job.tasks.count() == 1  # the in-flight task survived


@pytest.mark.django_db
def test_empty_scope_manual_job_finishes_instead_of_blocking_singleton(monkeypatch):
    # A MANUAL backfill with nothing to do (no reports at all) must reach a terminal status,
    # not get stuck in PENDING (which would block the singleton index forever).
    from radis.labels import tasks
    from radis.labels.factories import LabelFactory, LabelGroupFactory
    from radis.labels.models import LabelingTask

    LabelFactory.create(group=LabelGroupFactory.create())  # active label, but no reports
    job = LabelingJobFactory.create(
        trigger=LabelingJob.Trigger.MANUAL, status=LabelingJob.Status.PENDING
    )
    monkeypatch.setattr(LabelingTask, "delay", lambda self: None)

    tasks.process_labeling_job(job.pk)

    job.refresh_from_db()
    assert job.tasks.count() == 0
    assert job.status == LabelingJob.Status.SUCCESS
    assert job.status not in LabelingJob.ACTIVE_STATUSES  # singleton is freed
    # A fresh job can now be created (the index does not block it).
    LabelingJobFactory.create(status=LabelingJob.Status.PENDING)


@pytest.mark.django_db
def test_cancel_during_preparation_is_not_overwritten_by_pending_write(monkeypatch):
    # An admin cancel landing mid-PREPARING must win: the late PENDING write must not
    # resurrect the job (all-canceled tasks would wedge the singleton index forever).
    from radis.core.models import AnalysisTask
    from radis.core.utils.model_utils import cancel_job
    from radis.labels import tasks
    from radis.labels.factories import LabelFactory, LabelGroupFactory
    from radis.labels.models import LabelingTask
    from radis.reports.factories import ReportFactory

    LabelFactory.create(group=LabelGroupFactory.create())
    ReportFactory.create()
    job = LabelingJobFactory.create(
        trigger=LabelingJob.Trigger.MANUAL, status=LabelingJob.Status.PENDING
    )
    monkeypatch.setattr(LabelingTask, "delay", lambda self: None)

    stream = tasks._create_labeling_tasks_streaming

    def stream_then_cancel(j):
        stream(j)
        cancel_job(LabelingJob.objects.get(pk=j.pk))  # concurrent admin cancel mid-prep

    monkeypatch.setattr(tasks, "_create_labeling_tasks_streaming", stream_then_cancel)
    tasks.process_labeling_job(job.pk)

    job.refresh_from_db()
    assert job.status == LabelingJob.Status.CANCELED
    assert not job.tasks.filter(status=AnalysisTask.Status.PENDING).exists()


@pytest.mark.django_db
def test_cancel_during_preparation_is_not_overwritten_by_empty_scope_success(monkeypatch):
    # Same race on the empty-scope shortcut: cancel must not be overwritten by SUCCESS.
    from radis.core.utils.model_utils import cancel_job
    from radis.labels import tasks
    from radis.labels.factories import LabelFactory, LabelGroupFactory

    LabelFactory.create(group=LabelGroupFactory.create())  # active label, but no reports
    job = LabelingJobFactory.create(
        trigger=LabelingJob.Trigger.MANUAL, status=LabelingJob.Status.PENDING
    )

    stream = tasks._create_labeling_tasks_streaming

    def stream_then_cancel(j):
        stream(j)
        cancel_job(LabelingJob.objects.get(pk=j.pk))  # concurrent admin cancel mid-prep

    monkeypatch.setattr(tasks, "_create_labeling_tasks_streaming", stream_then_cancel)
    tasks.process_labeling_job(job.pk)

    job.refresh_from_db()
    assert job.status == LabelingJob.Status.CANCELED


@pytest.mark.django_db
def test_sweep_unblocks_canceling_labeling_job():
    # End-to-end: worker killed mid-task, then the job is canceled and gets stuck in
    # CANCELING. The sweep alone must bring it to CANCELED.
    from radis.core.models import AnalysisTask
    from radis.core.utils.recovery import sweep_stale_analysis_state
    from radis.labels.factories import LabelingTaskFactory

    job = LabelingJobFactory.create(status=LabelingJob.Status.CANCELING)
    LabelingTaskFactory.create(job=job, status=AnalysisTask.Status.IN_PROGRESS, queued_job=None)

    sweep_stale_analysis_state()

    job.refresh_from_db()
    assert job.status == LabelingJob.Status.CANCELED
    # The singleton index is free again: a new labeling job can be created.
    LabelingJobFactory.create(status=LabelingJob.Status.PENDING)
