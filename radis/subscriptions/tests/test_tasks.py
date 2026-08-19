import pytest
from adit_radis_shared.accounts.factories import GroupFactory, UserFactory
from procrastinate.contrib.django.models import ProcrastinateJob

from radis.reports.factories import ReportFactory
from radis.subscriptions import site as subscription_site
from radis.subscriptions.factories import (
    SubscriptionFactory,
    SubscriptionJobFactory,
    SubscriptionTaskFactory,
)
from radis.subscriptions.models import SubscriptionJob, SubscriptionTask
from radis.subscriptions.site import SubscriptionFilterProvider
from radis.subscriptions.tasks import process_subscription_job


@pytest.mark.django_db
def test_process_subscription_job_only_enqueues_tasks_after_job_is_pending(monkeypatch):
    """
    Same invariant as in #197: never enqueue tasks while the job is PREPARING.
    """

    user = UserFactory.create(is_active=True)
    group = GroupFactory.create()

    subscription = SubscriptionFactory.create(owner=user, group=group)
    job = SubscriptionJobFactory.create(subscription=subscription, owner=user)
    job.status = SubscriptionJob.Status.PREPARING
    job.save()

    doc_ids = ["SUB-DOC-1", "SUB-DOC-2"]
    for doc_id in doc_ids:
        ReportFactory.create(document_id=doc_id)

    provider = SubscriptionFilterProvider(
        name="dummy",
        filter=lambda _filters: doc_ids,
    )
    monkeypatch.setattr(subscription_site, "subscription_filter_provider", provider)

    enqueue_job_statuses: list[str] = []

    def fake_delay(self: SubscriptionTask) -> None:
        enqueue_job_statuses.append(self.job.status)

    monkeypatch.setattr(SubscriptionTask, "delay", fake_delay, raising=True)

    process_subscription_job(int(job.pk))

    assert enqueue_job_statuses
    assert all(status == SubscriptionJob.Status.PENDING for status in enqueue_job_statuses)


@pytest.mark.django_db(transaction=True)
def test_pending_job_resumes_enqueueing_after_crash(settings, monkeypatch):
    """A crash while enqueueing leaves a PENDING job with un-queued tasks. Running
    process_subscription_job again must enqueue them (nothing else repairs this)."""
    settings.PROCRASTINATE_READONLY_MODELS = False

    user = UserFactory.create(is_active=True)
    group = GroupFactory.create()
    subscription = SubscriptionFactory.create(owner=user, group=group)
    job = SubscriptionJobFactory.create(subscription=subscription, owner=user)
    job.status = SubscriptionJob.Status.PENDING
    job.save()

    tasks = [
        SubscriptionTaskFactory.create(
            job=job, status=SubscriptionTask.Status.PENDING, queued_job=None
        )
        for _ in range(2)
    ]
    # One task already has a queue row and one already succeeded: resume must skip both.
    queued_task = SubscriptionTaskFactory.create(
        job=job, status=SubscriptionTask.Status.PENDING, queued_job=None
    )
    queued_task.delay()
    queued_row_id = queued_task.queued_job_id
    done_task = SubscriptionTaskFactory.create(
        job=job, status=SubscriptionTask.Status.SUCCESS, queued_job=None
    )

    # Resuming must only enqueue; it must not search for reports again.
    def _fail_if_called(_filters):
        raise AssertionError("resume must not re-search for reports")

    monkeypatch.setattr(
        subscription_site,
        "subscription_filter_provider",
        SubscriptionFilterProvider(name="dummy", filter=_fail_if_called),
    )

    process_subscription_job(int(job.pk))

    assert job.tasks.count() == 4  # neither deleted nor duplicated
    for task in tasks:
        task.refresh_from_db()
        assert task.is_queued
        assert ProcrastinateJob.objects.filter(pk=task.queued_job_id).exists()
    queued_task.refresh_from_db()
    assert queued_task.queued_job_id == queued_row_id  # not enqueued a second time
    done_task.refresh_from_db()
    assert done_task.queued_job_id is None  # finished tasks are left alone

    job.refresh_from_db()
    assert job.status == SubscriptionJob.Status.PENDING


@pytest.mark.django_db(transaction=True)
def test_crash_while_enqueueing_rolls_back_flip_and_bookmark(settings, monkeypatch):
    """The PENDING flip, the last_refreshed bookmark and the task enqueues commit together.
    If enqueueing dies mid-loop everything rolls back, so the next run finds the same
    reports again instead of a half-enqueued job and a bookmark that skips them."""
    settings.PROCRASTINATE_READONLY_MODELS = False
    settings.SUBSCRIPTION_REFRESH_TASK_BATCH_SIZE = 1

    user = UserFactory.create(is_active=True)
    group = GroupFactory.create()
    subscription = SubscriptionFactory.create(owner=user, group=group)
    original_refreshed = subscription.last_refreshed
    job = SubscriptionJobFactory.create(subscription=subscription, owner=user)
    job.status = SubscriptionJob.Status.PREPARING
    job.save()

    doc_ids = ["SUB-DOC-1", "SUB-DOC-2"]  # 2 tasks with batch size 1
    for doc_id in doc_ids:
        ReportFactory.create(document_id=doc_id)
    monkeypatch.setattr(
        subscription_site,
        "subscription_filter_provider",
        SubscriptionFilterProvider(name="dummy", filter=lambda _filters: doc_ids),
    )

    real_delay = SubscriptionTask.delay
    calls = {"n": 0}

    def dies_on_second_enqueue(self):
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("worker killed mid-enqueue")
        real_delay(self)

    monkeypatch.setattr(SubscriptionTask, "delay", dies_on_second_enqueue, raising=True)

    with pytest.raises(RuntimeError):
        process_subscription_job(int(job.pk))

    subscription.refresh_from_db()
    assert subscription.last_refreshed == original_refreshed  # bookmark rolled back
    assert not job.tasks.filter(queued_job__isnull=False).exists()  # so did the first enqueue
    task_pks = list(job.tasks.values_list("pk", flat=True))
    assert not ProcrastinateJob.objects.filter(
        task_name="radis.subscriptions.tasks.process_subscription_task", args__task_id__in=task_pks
    ).exists()
