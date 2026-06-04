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
def test_finished_mail_is_skipped_when_owner_is_null():
    # Defensive: even with send_finished_mail set, an owner-less scan job must not crash.
    job = LabelingJob.objects.create(
        trigger=LabelingJob.Trigger.SCAN,
        status=LabelingJob.Status.SUCCESS,
        owner=None,
        send_finished_mail=True,
    )
    job._send_job_finished_mail()  # must be a no-op, not an AttributeError


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
