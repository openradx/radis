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
