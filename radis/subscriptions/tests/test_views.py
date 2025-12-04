from datetime import datetime, timedelta, timezone

import pytest
from adit_radis_shared.accounts.factories import GroupFactory, UserFactory
from django.core import mail
from django.test import Client
from django.utils import timezone as django_timezone

from radis.core.models import AnalysisTask
from radis.extractions.factories import OutputFieldFactory
from radis.reports.factories import LanguageFactory, ReportFactory
from radis.reports.models import Modality
from radis.subscriptions.factories import (
    FilterQuestionFactory,
    SubscribedItemFactory,
    SubscriptionFactory,
    SubscriptionJobFactory,
    SubscriptionTaskFactory,
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


# Email Notification Tests


@pytest.mark.django_db
def test_subscription_job_sends_email_when_enabled(client: Client):
    """Test that email is sent when send_finished_mail is True."""
    user = UserFactory.create(is_active=True, email="test@example.com")
    subscription = create_test_subscription(owner=user)
    subscription.send_finished_mail = True
    subscription.save()

    job = SubscriptionJobFactory(subscription=subscription, started_at=django_timezone.now())
    SubscriptionTaskFactory(job=job, status=AnalysisTask.Status.SUCCESS)

    # Create subscribed items
    language = LanguageFactory.create(code="en")
    for _ in range(3):
        SubscribedItemFactory(
            subscription=subscription, job=job, report=ReportFactory.create(language=language)
        )

    # Trigger email sending
    job.update_job_state()

    # Assertions
    assert len(mail.outbox) == 1
    email = mail.outbox[0]
    assert email.subject == f"Job {job} finished"
    assert email.to == [user.email]
    assert subscription.name in email.body


@pytest.mark.django_db
def test_subscription_job_no_email_when_disabled(client: Client):
    """Test that email is NOT sent when send_finished_mail is False."""
    user = UserFactory.create(is_active=True, email="test@example.com")
    subscription = create_test_subscription(owner=user)
    subscription.send_finished_mail = False
    subscription.save()

    job = SubscriptionJobFactory(subscription=subscription, started_at=django_timezone.now())
    SubscriptionTaskFactory(job=job, status=AnalysisTask.Status.SUCCESS)

    # Create subscribed items
    language = LanguageFactory.create(code="en")
    for _ in range(3):
        SubscribedItemFactory(
            subscription=subscription, job=job, report=ReportFactory.create(language=language)
        )

    # Trigger email sending
    job.update_job_state()

    # Assertions
    assert len(mail.outbox) == 0


@pytest.mark.django_db
def test_subscription_job_email_content_and_context(client: Client):
    """Test that email content and context are correct."""
    user = UserFactory.create(is_active=True, email="test@example.com")
    subscription = create_test_subscription(owner=user, name="Test Pneumothorax Subscription")
    subscription.send_finished_mail = True
    subscription.save()

    job = SubscriptionJobFactory(
        subscription=subscription, started_at=django_timezone.now() - timedelta(hours=1)
    )
    SubscriptionTaskFactory(job=job, status=AnalysisTask.Status.SUCCESS)
    language = LanguageFactory.create(code="en")

    # Items created AFTER job.started_at should appear in email
    new_item1 = SubscribedItemFactory(
        subscription=subscription, job=job, report=ReportFactory.create(language=language)
    )
    new_item1.created_at = django_timezone.now()
    new_item1.save()

    new_item2 = SubscribedItemFactory(
        subscription=subscription, job=job, report=ReportFactory.create(language=language)
    )
    new_item2.created_at = django_timezone.now()
    new_item2.save()

    # Item created BEFORE should NOT appear
    old_item = SubscribedItemFactory(
        subscription=subscription, report=ReportFactory.create(language=language)
    )
    old_item.created_at = django_timezone.now() - timedelta(hours=2)
    old_item.save()

    # Trigger email sending
    job.update_job_state()

    # Assertions
    assert len(mail.outbox) == 1
    email = mail.outbox[0]

    # Check subscription name appears
    assert "Test Pneumothorax Subscription" in email.body


# UI Badge Notification Tests


@pytest.mark.django_db
def test_subscription_badge_appears_for_first_time_view(client: Client):
    """Test that badge appears for subscriptions with null last_viewed_at."""
    user = UserFactory.create(is_active=True)
    subscription = create_test_subscription(owner=user)
    # last_viewed_at defaults to None
    assert subscription.last_viewed_at is None

    # Create subscribed items
    language = LanguageFactory.create(code="en")
    SubscribedItemFactory.create_batch(
        2, subscription=subscription, report=ReportFactory.create(language=language)
    )

    client.force_login(user)
    response = client.get("/subscriptions/")

    assert response.status_code == 200
    subscription_data = list(response.context["table"].data)[0]
    assert subscription_data.num_new_reports == 2
    assert "2 new" in response.content.decode()


@pytest.mark.django_db
def test_subscription_list_shows_badge_for_new_items(client: Client):
    """Test that badge shows correct count of new items."""
    user = UserFactory.create(is_active=True)
    subscription = create_test_subscription(owner=user)
    subscription.last_viewed_at = django_timezone.now() - timedelta(hours=5)
    subscription.save()
    language = LanguageFactory.create(code="en")

    # Create 3 items AFTER last_viewed_at
    for _ in range(3):
        item = SubscribedItemFactory(
            subscription=subscription, report=ReportFactory.create(language=language)
        )
        item.created_at = django_timezone.now()
        item.save()

    # Create 2 items BEFORE last_viewed_at (should not count)
    for _ in range(2):
        item = SubscribedItemFactory(
            subscription=subscription, report=ReportFactory.create(language=language)
        )
        item.created_at = django_timezone.now() - timedelta(hours=6)
        item.save()

    client.force_login(user)
    response = client.get("/subscriptions/")

    assert response.status_code == 200
    subscription_data = list(response.context["table"].data)[0]
    assert subscription_data.num_new_reports == 3
    assert "3 new" in response.content.decode()


@pytest.mark.django_db
def test_subscription_list_no_badge_when_no_new_items(client: Client):
    """Test that no badge appears when there are no new items."""
    user = UserFactory.create(is_active=True)
    subscription = create_test_subscription(owner=user)
    subscription.last_viewed_at = django_timezone.now()
    subscription.save()
    language = LanguageFactory.create(code="en")

    # Create items all BEFORE last_viewed_at
    item = SubscribedItemFactory(
        subscription=subscription, report=ReportFactory.create(language=language)
    )
    item.created_at = django_timezone.now() - timedelta(hours=1)
    item.save()

    client.force_login(user)
    response = client.get("/subscriptions/")

    assert response.status_code == 200
    subscription_data = list(response.context["table"].data)[0]
    assert subscription_data.num_new_reports == 0


@pytest.mark.django_db
def test_subscription_inbox_updates_last_viewed_at(client: Client):
    """Test that last_viewed_at is updated when viewing inbox."""
    user = UserFactory.create(is_active=True)
    subscription = create_test_subscription(owner=user)
    subscription.last_viewed_at = None
    subscription.save()

    client.force_login(user)

    before = django_timezone.now()
    response = client.get(f"/subscriptions/{subscription.pk}/inbox/")
    after = django_timezone.now()

    assert response.status_code == 200
    subscription.refresh_from_db()
    assert subscription.last_viewed_at is not None
    assert before <= subscription.last_viewed_at <= after


@pytest.mark.django_db
def test_subscription_badge_disappears_after_viewing_inbox(client: Client):
    """Test that badge disappears after viewing inbox."""
    user = UserFactory.create(is_active=True)
    subscription = create_test_subscription(owner=user)
    subscription.last_viewed_at = django_timezone.now() - timedelta(hours=2)
    subscription.save()
    language = LanguageFactory.create(code="en")

    # Create new items
    for _ in range(3):
        item = SubscribedItemFactory(
            subscription=subscription, report=ReportFactory.create(language=language)
        )
        item.created_at = django_timezone.now()
        item.save()

    client.force_login(user)

    # First list view - badge should appear
    response1 = client.get("/subscriptions/")
    assert response1.status_code == 200
    subscription_data1 = list(response1.context["table"].data)[0]
    assert subscription_data1.num_new_reports == 3
    assert "badge" in response1.content.decode()

    # View inbox - updates last_viewed_at
    response_inbox = client.get(f"/subscriptions/{subscription.pk}/inbox/")
    assert response_inbox.status_code == 200

    # Second list view - badge should be gone
    response2 = client.get("/subscriptions/")
    assert response2.status_code == 200
    subscription_data2 = list(response2.context["table"].data)[0]
    assert subscription_data2.num_new_reports == 0


@pytest.mark.django_db
def test_subscription_badge_reappears_with_new_items(client: Client):
    """Test that badge reappears when new items are added after viewing inbox."""
    user = UserFactory.create(is_active=True)
    subscription = create_test_subscription(owner=user)
    language = LanguageFactory.create(code="en")

    client.force_login(user)

    # View inbox to set last_viewed_at
    response1 = client.get(f"/subscriptions/{subscription.pk}/inbox/")
    assert response1.status_code == 200
    subscription.refresh_from_db()
    assert subscription.last_viewed_at is not None

    # Create new item with created_at AFTER the updated last_viewed_at
    item = SubscribedItemFactory(
        subscription=subscription, report=ReportFactory.create(language=language)
    )
    item.created_at = django_timezone.now()
    item.save()

    # Load subscription list
    response2 = client.get("/subscriptions/")
    assert response2.status_code == 200
    subscription_data = list(response2.context["table"].data)[0]
    assert subscription_data.num_new_reports == 1
    assert "1 new" in response2.content.decode()
