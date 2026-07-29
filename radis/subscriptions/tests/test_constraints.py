"""DB constraint tests for OutputField parentage and SubscribedItem uniqueness."""

import pytest
from django.db import IntegrityError

from radis.extractions.factories import ExtractionJobFactory, OutputFieldFactory
from radis.extractions.models import OutputField
from radis.subscriptions.factories import SubscribedItemFactory, SubscriptionFactory


@pytest.mark.django_db
def test_output_field_requires_exactly_one_parent_neither():
    with pytest.raises(IntegrityError):
        OutputField.objects.create(name="orphan", description="d", job=None, subscription=None)


@pytest.mark.django_db
def test_output_field_requires_exactly_one_parent_both():
    job = ExtractionJobFactory.create()
    subscription = SubscriptionFactory.create()
    with pytest.raises(IntegrityError):
        OutputField.objects.create(name="dual", description="d", job=job, subscription=subscription)


@pytest.mark.django_db
def test_output_field_name_unique_per_subscription():
    subscription = SubscriptionFactory.create()
    OutputFieldFactory.create(subscription=subscription, job=None, name="same_name")
    with pytest.raises(IntegrityError):
        OutputFieldFactory.create(subscription=subscription, job=None, name="same_name")


@pytest.mark.django_db
def test_subscribed_item_unique_per_subscription_and_report():
    item = SubscribedItemFactory.create()
    with pytest.raises(IntegrityError):
        SubscribedItemFactory.create(subscription=item.subscription, report=item.report)
