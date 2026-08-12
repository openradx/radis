"""Tests for subscriptions task orchestration (tasks.py):

- process_subscription_job: batching into SubscriptionTasks, report wiring,
  status transitions, last_refreshed update, and the missing-provider error
  branch.
- subscription_launcher: one PREPARING job per subscription, and no new job
  while one is still active.

The existing test_tasks.py already covers the "only enqueue after PENDING"
invariant; these focus on the build/launch behaviour.
"""

import pytest
from adit_radis_shared.accounts.factories import GroupFactory, UserFactory
from django.core.exceptions import ImproperlyConfigured

from radis.reports.factories import LanguageFactory, ReportFactory
from radis.subscriptions import site as subscription_site
from radis.subscriptions.factories import SubscriptionFactory, SubscriptionJobFactory
from radis.subscriptions.models import SubscriptionJob, SubscriptionTask
from radis.subscriptions.site import SubscriptionFilterProvider
from radis.subscriptions.tasks import process_subscription_job, subscription_launcher


def _preparing_job() -> SubscriptionJob:
    user = UserFactory.create(is_active=True)
    group = GroupFactory.create()
    language = LanguageFactory.create(code="en")
    subscription = SubscriptionFactory.create(owner=user, group=group, language=language)
    job = SubscriptionJobFactory.create(subscription=subscription, owner=user)
    job.status = SubscriptionJob.Status.PREPARING
    job.save()
    return job


@pytest.mark.django_db
def test_new_reports_are_batched_into_tasks(monkeypatch, settings):
    settings.SUBSCRIPTION_REFRESH_TASK_BATCH_SIZE = 2
    job = _preparing_job()

    doc_ids = ["S-1", "S-2", "S-3"]
    for doc_id in doc_ids:
        ReportFactory.create(document_id=doc_id)

    used = {"filter": 0}

    def _filter(_filters):
        used["filter"] += 1
        return doc_ids

    monkeypatch.setattr(
        subscription_site,
        "subscription_filter_provider",
        SubscriptionFilterProvider(name="f", filter=_filter),
    )
    monkeypatch.setattr(SubscriptionTask, "delay", lambda self: None, raising=True)

    process_subscription_job(int(job.pk))

    assert used == {"filter": 1}

    tasks = list(job.tasks.all())
    assert len(tasks) == 2  # ceil(3 / 2)
    total_reports = sum(t.reports.count() for t in tasks)
    assert total_reports == 3

    job.refresh_from_db()
    assert job.status == SubscriptionJob.Status.PENDING


@pytest.mark.django_db
def test_last_refreshed_is_advanced(monkeypatch):
    job = _preparing_job()
    before = job.subscription.last_refreshed

    monkeypatch.setattr(
        subscription_site,
        "subscription_filter_provider",
        SubscriptionFilterProvider(name="f", filter=lambda _f: []),
    )
    monkeypatch.setattr(SubscriptionTask, "delay", lambda self: None, raising=True)

    process_subscription_job(int(job.pk))

    job.subscription.refresh_from_db()
    assert job.subscription.last_refreshed > before
    # No documents -> no tasks created.
    assert job.tasks.count() == 0
    # A task-less job must complete immediately; if it stayed PENDING the
    # launcher would skip this subscription on every future refresh.
    job.refresh_from_db()
    assert job.status == SubscriptionJob.Status.SUCCESS


@pytest.mark.django_db
def test_missing_filter_provider_raises(monkeypatch):
    job = _preparing_job()
    monkeypatch.setattr(subscription_site, "subscription_filter_provider", None)

    with pytest.raises(ImproperlyConfigured):
        process_subscription_job(int(job.pk))


@pytest.mark.django_db
def test_subscription_launcher_creates_one_preparing_job_per_subscription(monkeypatch):
    # The launcher schedules job.delay via transaction.on_commit; stub it out so
    # no real Procrastinate deferral happens.
    monkeypatch.setattr(SubscriptionJob, "delay", lambda self: None, raising=True)

    subs = [SubscriptionFactory.create() for _ in range(3)]

    assert SubscriptionJob.objects.count() == 0
    subscription_launcher(0)

    jobs = SubscriptionJob.objects.all()
    assert jobs.count() == 3
    assert {j.subscription.pk for j in jobs} == {s.pk for s in subs}
    assert all(j.status == SubscriptionJob.Status.PREPARING for j in jobs)
    # Owner is copied from the subscription.
    for job in jobs:
        assert job.owner_id == job.subscription.owner_id


@pytest.mark.django_db
def test_subscription_launcher_skips_subscription_with_active_job(monkeypatch):
    monkeypatch.setattr(SubscriptionJob, "delay", lambda self: None, raising=True)

    subscription = SubscriptionFactory.create()
    SubscriptionJobFactory.create(
        subscription=subscription,
        owner=subscription.owner,
        status=SubscriptionJob.Status.IN_PROGRESS,
    )

    subscription_launcher(0)

    # No second job while one is still active.
    assert subscription.jobs.count() == 1


@pytest.mark.django_db
def test_refired_prep_job_does_not_duplicate_tasks(monkeypatch):
    job = _preparing_job()
    ReportFactory.create(document_id="S-REFIRE-1")

    monkeypatch.setattr(
        subscription_site,
        "subscription_filter_provider",
        SubscriptionFilterProvider(name="f", filter=lambda _f: ["S-REFIRE-1"]),
    )
    monkeypatch.setattr(SubscriptionTask, "delay", lambda self: None, raising=True)

    process_subscription_job(int(job.pk))
    assert job.tasks.count() == 1

    # Simulate a crash after task creation but before the PENDING switch, then re-fire.
    SubscriptionJob.objects.filter(pk=job.pk).update(status=SubscriptionJob.Status.PREPARING)
    process_subscription_job(int(job.pk))

    job.refresh_from_db()
    assert job.tasks.count() == 1  # wiped and recreated, not duplicated


@pytest.mark.django_db
def test_prep_job_in_unexpected_status_is_ignored(monkeypatch):
    job = _preparing_job()
    SubscriptionJob.objects.filter(pk=job.pk).update(status=SubscriptionJob.Status.SUCCESS)

    process_subscription_job(int(job.pk))  # must not raise

    job.refresh_from_db()
    assert job.status == SubscriptionJob.Status.SUCCESS
    assert job.tasks.count() == 0
