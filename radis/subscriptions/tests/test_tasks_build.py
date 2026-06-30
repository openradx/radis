"""Tests for subscriptions task orchestration (tasks.py):

- process_subscription_job: query vs. filter path, batching into
  SubscriptionTasks, report wiring, status transitions, last_refreshed update,
  and the missing-provider error branches.
- subscription_launcher: one PREPARING job per subscription.

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
from radis.subscriptions.site import SubscriptionFilterProvider, SubscriptionRetrievalProvider
from radis.subscriptions.tasks import process_subscription_job, subscription_launcher


def _preparing_job(query: str) -> SubscriptionJob:
    user = UserFactory.create(is_active=True)
    group = GroupFactory.create()
    language = LanguageFactory.create(code="en")
    subscription = SubscriptionFactory.create(
        owner=user, group=group, language=language, query=query
    )
    job = SubscriptionJobFactory.create(subscription=subscription, owner=user)
    job.status = SubscriptionJob.Status.PREPARING
    job.save()
    return job


@pytest.mark.django_db
def test_filter_path_used_when_query_empty(monkeypatch, settings):
    settings.SUBSCRIPTION_REFRESH_TASK_BATCH_SIZE = 2
    job = _preparing_job(query="")

    doc_ids = ["S-1", "S-2", "S-3"]
    for doc_id in doc_ids:
        ReportFactory.create(document_id=doc_id)

    used = {"filter": 0, "retrieve": 0}

    def _filter(_filters):
        used["filter"] += 1
        return doc_ids

    def _retrieve(_search):
        used["retrieve"] += 1
        return doc_ids

    monkeypatch.setattr(
        subscription_site,
        "subscription_filter_provider",
        SubscriptionFilterProvider(name="f", filter=_filter),
    )
    monkeypatch.setattr(
        subscription_site,
        "subscription_retrieval_provider",
        SubscriptionRetrievalProvider(name="r", retrieve=_retrieve),
    )
    monkeypatch.setattr(SubscriptionTask, "delay", lambda self: None, raising=True)

    process_subscription_job(int(job.pk))

    # Empty query -> filter provider, never the retrieval provider.
    assert used == {"filter": 1, "retrieve": 0}

    tasks = list(job.tasks.all())
    assert len(tasks) == 2  # ceil(3 / 2)
    total_reports = sum(t.reports.count() for t in tasks)
    assert total_reports == 3

    job.refresh_from_db()
    assert job.status == SubscriptionJob.Status.PENDING


@pytest.mark.django_db
def test_retrieval_path_used_when_query_present(monkeypatch, settings):
    settings.SUBSCRIPTION_REFRESH_TASK_BATCH_SIZE = 100
    job = _preparing_job(query="pneumonia")

    doc_ids = ["Q-1", "Q-2"]
    for doc_id in doc_ids:
        ReportFactory.create(document_id=doc_id)

    used = {"filter": 0, "retrieve": 0}
    monkeypatch.setattr(
        subscription_site,
        "subscription_filter_provider",
        SubscriptionFilterProvider(
            name="f", filter=lambda _f: used.__setitem__("filter", used["filter"] + 1) or doc_ids
        ),
    )
    monkeypatch.setattr(
        subscription_site,
        "subscription_retrieval_provider",
        SubscriptionRetrievalProvider(
            name="r",
            retrieve=lambda _s: used.__setitem__("retrieve", used["retrieve"] + 1) or doc_ids,
        ),
    )
    monkeypatch.setattr(SubscriptionTask, "delay", lambda self: None, raising=True)

    process_subscription_job(int(job.pk))

    # Non-empty query -> retrieval provider, never the filter provider.
    assert used == {"filter": 0, "retrieve": 1}
    assert job.tasks.count() == 1
    assert job.tasks.get().reports.count() == 2


@pytest.mark.django_db
def test_last_refreshed_is_advanced(monkeypatch):
    job = _preparing_job(query="")
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


@pytest.mark.django_db
def test_missing_filter_provider_raises(monkeypatch):
    job = _preparing_job(query="")
    monkeypatch.setattr(subscription_site, "subscription_filter_provider", None)

    with pytest.raises(ImproperlyConfigured):
        process_subscription_job(int(job.pk))


@pytest.mark.django_db
def test_missing_retrieval_provider_raises(monkeypatch):
    job = _preparing_job(query="pneumonia")
    monkeypatch.setattr(subscription_site, "subscription_retrieval_provider", None)

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
