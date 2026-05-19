import pytest
from adit_radis_shared.accounts.factories import GroupFactory, UserFactory

from radis.reports.factories import ReportFactory
from radis.subscriptions import site as subscription_site
from radis.subscriptions.factories import SubscriptionFactory, SubscriptionJobFactory
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

    subscription = SubscriptionFactory.create(owner=user, group=group, query="")
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
