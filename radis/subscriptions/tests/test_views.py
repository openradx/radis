from datetime import datetime, timedelta, timezone

import pytest
from adit_radis_shared.accounts.factories import GroupFactory, UserFactory
from django.test import Client

from radis.extractions.factories import OutputFieldFactory
from radis.reports.factories import LanguageFactory, ReportFactory
from radis.reports.models import Modality
from radis.subscriptions.factories import (
    FilterQuestionFactory,
    SubscribedItemFactory,
    SubscriptionFactory,
)
from radis.subscriptions.models import Subscription


def create_test_subscription(owner=None, group=None, name=None):
    if not owner:
        owner = UserFactory.create(is_active=True)
    if not group:
        group = GroupFactory.create()

    if group not in owner.groups.all():
        owner.groups.add(group)

    language = LanguageFactory.create(code="en")

    kwargs = {"owner": owner, "group": group, "language": language}
    if name:
        kwargs["name"] = name
    return SubscriptionFactory.create(**kwargs)


@pytest.mark.django_db
def test_subscription_list_view(client: Client):
    user = UserFactory.create(is_active=True)
    create_test_subscription(owner=user)
    client.force_login(user)
    response = client.get("/subscriptions/")
    assert response.status_code == 200


@pytest.mark.django_db
def test_subscription_list_view_unauthenticated(client: Client):
    response = client.get("/subscriptions/")
    assert response.status_code == 302
    assert "/accounts/login/" in response["Location"]


@pytest.mark.django_db
def test_subscription_list_view_filters_by_owner(client: Client):
    user1 = UserFactory.create(is_active=True)
    user2 = UserFactory.create(is_active=True)

    subscription1 = create_test_subscription(owner=user1)
    subscription2 = create_test_subscription(owner=user2)

    client.force_login(user1)
    response = client.get("/subscriptions/")

    assert response.status_code == 200
    assert subscription1 in response.context["table"].data
    assert subscription2 not in response.context["table"].data


@pytest.mark.django_db
def test_subscription_create_view_get(client: Client):
    user = UserFactory.create(is_active=True)
    client.force_login(user)
    response = client.get("/subscriptions/create/")
    assert response.status_code == 200


@pytest.mark.django_db
def test_subscription_create_view_unauthenticated(client: Client):
    response = client.get("/subscriptions/create/")
    assert response.status_code == 302
    assert "/accounts/login/" in response["Location"]


@pytest.mark.django_db
def test_subscription_create_view_post_valid(client: Client):
    user = UserFactory.create(is_active=True)
    group = GroupFactory.create()
    user.groups.add(group)
    user.active_group = group
    user.save()

    language = LanguageFactory.create(code="en")
    modality = Modality.objects.create(code="CT", filterable=True)

    client.force_login(user)

    data = {
        "name": "Test Subscription",
        "query": "test query",
        "language": language.pk,
        "modalities": [modality.pk],
        "study_description": "Test study",
        "patient_sex": "M",
        "age_from": 30,
        "age_till": 60,
        "patient_id": "12345",
        "send_finished_mail": True,
        "filter_questions-TOTAL_FORMS": "1",
        "filter_questions-INITIAL_FORMS": "0",
        "filter_questions-MIN_NUM_FORMS": "0",
        "filter_questions-MAX_NUM_FORMS": "3",
        "filter_questions-0-question": "Does the report contain pneumothorax?",
        "filter_questions-0-expected_answer": "Y",
        "output_fields-TOTAL_FORMS": "1",
        "output_fields-INITIAL_FORMS": "0",
        "output_fields-MIN_NUM_FORMS": "0",
        "output_fields-MAX_NUM_FORMS": "10",
        "output_fields-0-name": "Pneumothorax status",
        "output_fields-0-description": "Extract pneumothorax related findings",
        "output_fields-0-output_type": "T",
    }

    response = client.post("/subscriptions/create/", data)
    assert response.status_code == 302

    assert Subscription.objects.filter(name="Test Subscription").exists()


@pytest.mark.django_db
def test_subscription_create_view_ignores_empty_filter_question(client: Client):
    user = UserFactory.create(is_active=True)
    group = GroupFactory.create()
    user.groups.add(group)
    user.active_group = group
    user.save()

    language = LanguageFactory.create(code="en")

    client.force_login(user)

    data = {
        "name": "Subscription Without Filter",
        "provider": "test_provider",
        "language": language.pk,
        "query": "",
        "filter_questions-TOTAL_FORMS": "1",
        "filter_questions-INITIAL_FORMS": "0",
        "filter_questions-MIN_NUM_FORMS": "0",
        "filter_questions-MAX_NUM_FORMS": "3",
        "filter_questions-0-question": "",
        "filter_questions-0-expected_answer": "",
        "output_fields-TOTAL_FORMS": "0",
        "output_fields-INITIAL_FORMS": "0",
        "output_fields-MIN_NUM_FORMS": "0",
        "output_fields-MAX_NUM_FORMS": "10",
    }

    response = client.post("/subscriptions/create/", data)
    assert response.status_code == 302

    subscription = Subscription.objects.get(name="Subscription Without Filter")
    assert subscription.filter_questions.count() == 0


@pytest.mark.django_db
def test_subscription_create_view_post_duplicate_name(client: Client):
    user = UserFactory.create(is_active=True)
    group = GroupFactory.create()
    user.groups.add(group)
    user.active_group = group
    user.save()

    create_test_subscription(owner=user, name="Duplicate Name")

    client.force_login(user)

    language = LanguageFactory.create(code="en")

    data = {
        "name": "Duplicate Name",
        "provider": "test_provider",
        "language": language.pk,
        "query": "",
        "filter_questions-TOTAL_FORMS": "0",
        "filter_questions-INITIAL_FORMS": "0",
        "filter_questions-MIN_NUM_FORMS": "0",
        "filter_questions-MAX_NUM_FORMS": "3",
        "output_fields-TOTAL_FORMS": "0",
        "output_fields-INITIAL_FORMS": "0",
        "output_fields-MIN_NUM_FORMS": "0",
        "output_fields-MAX_NUM_FORMS": "10",
    }

    response = client.post("/subscriptions/create/", data)
    assert response.status_code == 200
    assert "form" in response.context
    assert response.context["form"].errors


@pytest.mark.django_db
def test_subscription_detail_view(client: Client):
    user = UserFactory.create(is_active=True)
    subscription = create_test_subscription(owner=user)
    client.force_login(user)
    response = client.get(f"/subscriptions/{subscription.pk}/")
    assert response.status_code == 200


@pytest.mark.django_db
def test_subscription_detail_view_unauthenticated(client: Client):
    subscription = create_test_subscription()
    response = client.get(f"/subscriptions/{subscription.pk}/")
    assert response.status_code == 302
    assert "/accounts/login/" in response["Location"]


@pytest.mark.django_db
def test_subscription_detail_view_unauthorized(client: Client):
    user = UserFactory.create(is_active=True)
    other_user = UserFactory.create(is_active=True)
    subscription = create_test_subscription(owner=other_user)
    client.force_login(user)
    response = client.get(f"/subscriptions/{subscription.pk}/")
    assert response.status_code == 404


@pytest.mark.django_db
def test_subscription_update_view_get(client: Client):
    user = UserFactory.create(is_active=True)
    subscription = create_test_subscription(owner=user)
    client.force_login(user)
    response = client.get(f"/subscriptions/{subscription.pk}/update/")
    assert response.status_code == 200


@pytest.mark.django_db
def test_subscription_update_view_unauthorized(client: Client):
    user = UserFactory.create(is_active=True)
    other_user = UserFactory.create(is_active=True)
    subscription = create_test_subscription(owner=other_user)
    client.force_login(user)
    response = client.get(f"/subscriptions/{subscription.pk}/update/")
    assert response.status_code == 404


@pytest.mark.django_db
def test_subscription_update_view_post_valid(client: Client):
    user = UserFactory.create(is_active=True)
    subscription = create_test_subscription(owner=user, name="Original Name")
    question = FilterQuestionFactory.create(subscription=subscription)
    output_field = OutputFieldFactory.create(subscription=subscription, job=None)

    client.force_login(user)

    data = {
        "name": "Updated Name",
        "query": "updated query",
        "study_description": "Updated study",
        "patient_sex": "F",
        "send_finished_mail": False,
        "filter_questions-TOTAL_FORMS": "1",
        "filter_questions-INITIAL_FORMS": "1",
        "filter_questions-MIN_NUM_FORMS": "0",
        "filter_questions-MAX_NUM_FORMS": "3",
        "filter_questions-0-id": question.pk,
        "filter_questions-0-question": "Updated question?",
        "filter_questions-0-expected_answer": "N",
        "output_fields-TOTAL_FORMS": "1",
        "output_fields-INITIAL_FORMS": "1",
        "output_fields-MIN_NUM_FORMS": "0",
        "output_fields-MAX_NUM_FORMS": "10",
        "output_fields-0-id": output_field.pk,
        "output_fields-0-name": "Volume",
        "output_fields-0-description": "Volume description",
        "output_fields-0-output_type": "N",
    }

    response = client.post(f"/subscriptions/{subscription.pk}/update/", data)
    assert response.status_code == 302

    subscription.refresh_from_db()
    assert subscription.name == "Updated Name"


@pytest.mark.django_db
def test_subscription_delete_view_post(client: Client):
    user = UserFactory.create(is_active=True)
    subscription = create_test_subscription(owner=user)
    subscription_pk = subscription.pk

    client.force_login(user)
    response = client.post(f"/subscriptions/{subscription.pk}/delete/")

    assert response.status_code == 302
    assert not Subscription.objects.filter(pk=subscription_pk).exists()


@pytest.mark.django_db
def test_subscription_delete_view_unauthorized(client: Client):
    user = UserFactory.create(is_active=True)
    other_user = UserFactory.create(is_active=True)
    subscription = create_test_subscription(owner=other_user)
    client.force_login(user)
    response = client.post(f"/subscriptions/{subscription.pk}/delete/")
    assert response.status_code == 404


@pytest.mark.django_db
def test_subscription_inbox_view(client: Client):
    user = UserFactory.create(is_active=True)
    subscription = create_test_subscription(owner=user)
    client.force_login(user)
    response = client.get(f"/subscriptions/{subscription.pk}/inbox/")
    assert response.status_code == 200


@pytest.mark.django_db
def test_subscription_inbox_view_unauthorized(client: Client):
    user = UserFactory.create(is_active=True)
    other_user = UserFactory.create(is_active=True)
    subscription = create_test_subscription(owner=other_user)
    client.force_login(user)
    response = client.get(f"/subscriptions/{subscription.pk}/inbox/")
    assert response.status_code == 404


@pytest.mark.django_db
def test_subscription_inbox_view_staff_access(client: Client):
    owner = UserFactory.create(is_active=True)
    staff_user = UserFactory.create(is_active=True, is_staff=True)
    subscription = create_test_subscription(owner=owner)

    client.force_login(staff_user)
    response = client.get(f"/subscriptions/{subscription.pk}/inbox/")
    assert response.status_code == 200


@pytest.mark.django_db
def test_subscription_help_view(client: Client):
    user = UserFactory.create(is_active=True)
    client.force_login(user)
    response = client.get("/subscriptions/help/", HTTP_HX_REQUEST="true")
    assert response.status_code == 200


@pytest.mark.django_db
def test_subscription_help_view_unauthenticated(client: Client):
    user = UserFactory.create(is_active=True)
    client.force_login(user)
    response = client.get("/subscriptions/help/")
    assert response.status_code == 400


@pytest.mark.django_db
def test_unauthenticated_access_redirects_to_login(client: Client):
    user = UserFactory.create(is_active=True)
    subscription = create_test_subscription(owner=user)

    endpoints = [
        "/subscriptions/",
        "/subscriptions/create/",
        f"/subscriptions/{subscription.pk}/",
        f"/subscriptions/{subscription.pk}/update/",
        f"/subscriptions/{subscription.pk}/delete/",
        f"/subscriptions/{subscription.pk}/inbox/",
    ]

    for endpoint in endpoints:
        response = client.get(endpoint)
        assert response.status_code == 302
        assert "/accounts/login/" in response["Location"]


# Subscription Inbox Pagination and Sorting Tests


@pytest.mark.django_db
def test_subscription_inbox_pagination(client: Client):
    """Test that pagination works correctly in subscription inbox."""

    user = UserFactory.create(is_active=True)
    subscription = create_test_subscription(owner=user)
    language = LanguageFactory.create(code="en")

    # Create 25 subscribed items
    base_time = datetime.now(timezone.utc)
    for i in range(25):
        item = SubscribedItemFactory.create(
            subscription=subscription, report=ReportFactory.create(language=language)
        )
        # Set created_at to ensure consistent ordering
        item.created_at = base_time - timedelta(hours=i)
        item.save()

    client.force_login(user)

    # Test first page with default page size (10)
    response = client.get(f"/subscriptions/{subscription.pk}/inbox/")
    assert response.status_code == 200
    assert len(response.context["object_list"]) == 10
    assert response.context["page_obj"].number == 1
    assert response.context["page_obj"].paginator.num_pages == 3

    # Test second page
    response = client.get(f"/subscriptions/{subscription.pk}/inbox/?page=2")
    assert response.status_code == 200
    assert response.context["page_obj"].number == 2

    # Test custom page size
    response = client.get(f"/subscriptions/{subscription.pk}/inbox/?per_page=25")
    assert len(response.context["object_list"]) == 25
    assert response.context["page_obj"].paginator.num_pages == 1


@pytest.mark.django_db
def test_subscription_inbox_sorting_by_created_date(client: Client):
    """Test sorting by created_at date."""

    user = UserFactory.create(is_active=True)
    subscription = create_test_subscription(owner=user)
    language = LanguageFactory.create(code="en")

    # Create items with different created_at times
    item1 = SubscribedItemFactory.create(
        subscription=subscription, report=ReportFactory.create(language=language)
    )
    item2 = SubscribedItemFactory.create(
        subscription=subscription, report=ReportFactory.create(language=language)
    )
    item3 = SubscribedItemFactory.create(
        subscription=subscription, report=ReportFactory.create(language=language)
    )

    # Manually set created_at to ensure order
    base_time = datetime.now(timezone.utc)
    item1.created_at = base_time - timedelta(days=2)
    item1.save()
    item2.created_at = base_time - timedelta(days=1)
    item2.save()
    item3.created_at = base_time
    item3.save()

    client.force_login(user)

    # Test descending (newest first - default)
    response = client.get(f"/subscriptions/{subscription.pk}/inbox/?sort_by=created_at&order=desc")
    assert response.status_code == 200
    items = list(response.context["object_list"])
    assert items[0].pk == item3.pk
    assert items[1].pk == item2.pk
    assert items[2].pk == item1.pk

    # Test ascending (oldest first)
    response = client.get(f"/subscriptions/{subscription.pk}/inbox/?sort_by=created_at&order=asc")
    assert response.status_code == 200
    items = list(response.context["object_list"])
    assert items[0].pk == item1.pk
    assert items[1].pk == item2.pk
    assert items[2].pk == item3.pk


@pytest.mark.django_db
def test_subscription_inbox_sorting_by_study_date(client: Client):
    """Test sorting by study date."""

    user = UserFactory.create(is_active=True)
    subscription = create_test_subscription(owner=user)

    # Create reports with different study dates
    base_time = datetime.now(timezone.utc)
    language = LanguageFactory.create(code="en")
    report1 = ReportFactory.create(language=language, study_datetime=base_time - timedelta(days=10))
    report2 = ReportFactory.create(language=language, study_datetime=base_time - timedelta(days=5))
    report3 = ReportFactory.create(language=language, study_datetime=base_time - timedelta(days=1))

    item1 = SubscribedItemFactory.create(subscription=subscription, report=report1)
    item2 = SubscribedItemFactory.create(subscription=subscription, report=report2)
    item3 = SubscribedItemFactory.create(subscription=subscription, report=report3)

    client.force_login(user)

    # Test descending (newest study date first)
    response = client.get(f"/subscriptions/{subscription.pk}/inbox/?sort_by=study_date&order=desc")
    assert response.status_code == 200
    items = list(response.context["object_list"])
    assert items[0].pk == item3.pk
    assert items[1].pk == item2.pk
    assert items[2].pk == item1.pk

    # Test ascending (oldest study date first)
    response = client.get(f"/subscriptions/{subscription.pk}/inbox/?sort_by=study_date&order=asc")
    assert response.status_code == 200
    items = list(response.context["object_list"])
    assert items[0].pk == item1.pk
    assert items[1].pk == item2.pk
    assert items[2].pk == item3.pk


@pytest.mark.django_db
def test_subscription_inbox_invalid_sort_parameters(client: Client):
    """Test that invalid sort parameters fall back to defaults."""

    user = UserFactory.create(is_active=True)
    subscription = create_test_subscription(owner=user)
    language = LanguageFactory.create(code="en")

    SubscribedItemFactory.create(
        subscription=subscription, report=ReportFactory.create(language=language)
    )

    client.force_login(user)

    # Invalid sort_by should default to created_at
    response = client.get(
        f"/subscriptions/{subscription.pk}/inbox/?sort_by=invalid_field&order=desc"
    )
    assert response.status_code == 200
    assert response.context["current_sort_by"] == "created_at"

    # Invalid order should default to desc
    response = client.get(
        f"/subscriptions/{subscription.pk}/inbox/?sort_by=created_at&order=invalid"
    )
    assert response.status_code == 200
    assert response.context["current_order"] == "desc"


@pytest.mark.django_db
def test_subscription_inbox_filtering_by_patient_id(client: Client):
    """Test filtering by patient ID."""

    user = UserFactory.create(is_active=True)
    subscription = create_test_subscription(owner=user)
    language = LanguageFactory.create(code="en")
    report1 = ReportFactory.create(language=language, patient_id="12345")
    report2 = ReportFactory.create(language=language, patient_id="67890")
    report3 = ReportFactory.create(language=language, patient_id="54321")

    item1 = SubscribedItemFactory.create(subscription=subscription, report=report1)
    item2 = SubscribedItemFactory.create(subscription=subscription, report=report2)
    item3 = SubscribedItemFactory.create(subscription=subscription, report=report3)

    client.force_login(user)

    # Filter by patient_id (partial match)
    response = client.get(f"/subscriptions/{subscription.pk}/inbox/?patient_id=123")
    assert response.status_code == 200
    items = list(response.context["object_list"])
    assert len(items) == 1  # Should match "12345"
    assert item1 in items
    assert item2 and item3 not in items


@pytest.mark.django_db
def test_subscription_inbox_filtering_by_date_range(client: Client):
    """Test filtering by study date range."""

    user = UserFactory.create(is_active=True)
    subscription = create_test_subscription(owner=user)

    base_date = datetime.now(timezone.utc)
    language = LanguageFactory.create(code="en")
    report1 = ReportFactory.create(language=language, study_datetime=base_date - timedelta(days=10))
    report2 = ReportFactory.create(language=language, study_datetime=base_date - timedelta(days=5))
    report3 = ReportFactory.create(language=language, study_datetime=base_date - timedelta(days=1))

    item1 = SubscribedItemFactory.create(subscription=subscription, report=report1)
    item2 = SubscribedItemFactory.create(subscription=subscription, report=report2)
    item3 = SubscribedItemFactory.create(subscription=subscription, report=report3)

    client.force_login(user)

    # Filter by date range
    date_from = (base_date - timedelta(days=7)).strftime("%Y-%m-%d")
    date_till = (base_date - timedelta(days=2)).strftime("%Y-%m-%d")

    response = client.get(
        f"/subscriptions/{subscription.pk}/inbox/?study_date_from={date_from}&study_date_till={date_till}"
    )
    assert response.status_code == 200
    items = list(response.context["object_list"])
    assert len(items) == 1
    assert item2 in items
    assert item1 and item3 not in items


@pytest.mark.django_db
def test_subscription_inbox_combined_filter_and_sort(client: Client):
    """Test that filtering and sorting work together."""

    user = UserFactory.create(is_active=True)
    subscription = create_test_subscription(owner=user)

    # Create items with same patient but different study dates
    base_date = datetime.now(timezone.utc)
    language = LanguageFactory.create(code="en")
    report1 = ReportFactory.create(
        language=language, patient_id="12345", study_datetime=base_date - timedelta(days=5)
    )
    report2 = ReportFactory.create(
        language=language, patient_id="12345", study_datetime=base_date - timedelta(days=1)
    )
    report3 = ReportFactory.create(language=language, patient_id="67890", study_datetime=base_date)

    item1 = SubscribedItemFactory.create(subscription=subscription, report=report1)
    item2 = SubscribedItemFactory.create(subscription=subscription, report=report2)
    item3 = SubscribedItemFactory.create(subscription=subscription, report=report3)

    client.force_login(user)

    # Filter by patient_id and sort by study_date ascending
    response = client.get(
        f"/subscriptions/{subscription.pk}/inbox/?patient_id=12345&sort_by=study_date&order=asc"
    )
    assert response.status_code == 200
    items = list(response.context["object_list"])
    assert len(items) == 2
    assert items[0].pk == item1.pk  # Older study date
    assert items[1].pk == item2.pk  # Newer study date
    assert item3 not in items  # Different patient
