import time
from datetime import timedelta

import pytest
from django.utils import timezone

from radis.labels.factories import LabelFactory, LabelGroupFactory, LabelingJobFactory
from radis.labels.models import LabelingJob, LabelingScanCheckpoint
from radis.reports.factories import ReportFactory


def _now_ts():
    return int(time.time())


@pytest.mark.django_db
def test_first_run_sets_checkpoint_and_creates_no_job(monkeypatch):
    from radis.labels import tasks

    monkeypatch.setattr(LabelingJob, "delay", lambda self: None)
    tasks.incremental_label_scan(_now_ts())

    cp = LabelingScanCheckpoint.objects.get(pk=1)
    assert cp.last_scanned_at is not None
    assert not LabelingJob.objects.exists()


@pytest.mark.django_db
def test_active_job_guard_skips_without_advancing(monkeypatch):
    from radis.labels import tasks

    LabelingScanCheckpoint.objects.create(last_scanned_at=timezone.now())
    before = LabelingScanCheckpoint.objects.get(pk=1).last_scanned_at
    LabelingJobFactory.create(status=LabelingJob.Status.IN_PROGRESS)

    monkeypatch.setattr(LabelingJob, "delay", lambda self: None)
    tasks.incremental_label_scan(_now_ts())

    after = LabelingScanCheckpoint.objects.get(pk=1).last_scanned_at
    assert after == before
    assert LabelingJob.objects.filter(trigger=LabelingJob.Trigger.SCAN).count() == 0


@pytest.mark.django_db
def test_no_new_reports_advances_checkpoint_without_job(monkeypatch):
    from radis.labels import tasks

    LabelFactory.create(group=LabelGroupFactory.create())
    # Clearly in the past so the new checkpoint (tick time, second-resolution) is strictly later.
    before = timezone.now() - timedelta(hours=1)
    LabelingScanCheckpoint.objects.create(last_scanned_at=before)

    monkeypatch.setattr(LabelingJob, "delay", lambda self: None)
    tasks.incremental_label_scan(_now_ts())

    after = LabelingScanCheckpoint.objects.get(pk=1).last_scanned_at
    assert after > before
    assert not LabelingJob.objects.exists()


@pytest.mark.django_db
def test_new_reports_create_scan_job_and_advance(monkeypatch):
    from radis.labels import tasks

    LabelFactory.create(group=LabelGroupFactory.create())
    LabelingScanCheckpoint.objects.create(last_scanned_at=timezone.now() - timedelta(hours=1))
    ReportFactory.create()  # created now, after checkpoint

    delayed = []
    monkeypatch.setattr(LabelingJob, "delay", lambda self: delayed.append(self.pk))
    tasks.incremental_label_scan(_now_ts())

    job = LabelingJob.objects.get(trigger=LabelingJob.Trigger.SCAN)
    assert job.scan_from is not None
    assert delayed == [job.pk]


@pytest.mark.django_db
def test_no_active_labels_skips(monkeypatch):
    from radis.labels import tasks

    # checkpoint set, a report exists, but NO active labels -> no job, checkpoint NOT advanced
    frozen = timezone.now() - timedelta(hours=1)
    LabelingScanCheckpoint.objects.create(last_scanned_at=frozen)
    ReportFactory.create()

    monkeypatch.setattr(LabelingJob, "delay", lambda self: None)
    tasks.incremental_label_scan(_now_ts())

    assert not LabelingJob.objects.exists()
    # Checkpoint must stay frozen (not advanced) when there are no active labels.
    assert LabelingScanCheckpoint.objects.get(pk=1).last_scanned_at == frozen
