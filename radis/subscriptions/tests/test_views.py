import pytest
from adit_radis_shared.accounts.factories import GroupFactory, UserFactory
from django.test import Client

from radis.reports.factories import LanguageFactory
from radis.reports.models import Modality
from radis.subscriptions.factories import (
    QuestionFactory,
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
        "provider": "test_provider",
        "query": "test query",
        "language": language.pk,
        "modalities": [modality.pk],
        "study_description": "Test study",
        "patient_sex": "M",
        "age_from": 30,
        "age_till": 60,
        "patient_id": "12345",
        "send_finished_mail": True,
        "questions-TOTAL_FORMS": "1",
        "questions-INITIAL_FORMS": "0",
        "questions-MIN_NUM_FORMS": "0",
        "questions-MAX_NUM_FORMS": "3",
        "questions-0-question": "What is the diagnosis?",
    }

    response = client.post("/subscriptions/create/", data)
    assert response.status_code == 302

    assert Subscription.objects.filter(name="Test Subscription").exists()


@pytest.mark.django_db
def test_subscription_create_view_post_duplicate_name(client: Client):
    user = UserFactory.create(is_active=True)
    group = GroupFactory.create()
    user.groups.add(group)
    user.active_group = group
    user.save()

    create_test_subscription(owner=user, name="Duplicate Name")

    client.force_login(user)

    data = {
        "name": "Duplicate Name",
        "provider": "test_provider",
        "questions-TOTAL_FORMS": "0",
        "questions-INITIAL_FORMS": "0",
        "questions-MIN_NUM_FORMS": "0",
        "questions-MAX_NUM_FORMS": "3",
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
    question = QuestionFactory.create(subscription=subscription)

    client.force_login(user)

    data = {
        "name": "Updated Name",
        "provider": subscription.provider,
        "query": "updated query",
        "study_description": "Updated study",
        "patient_sex": "F",
        "send_finished_mail": False,
        "questions-TOTAL_FORMS": "1",
        "questions-INITIAL_FORMS": "1",
        "questions-MIN_NUM_FORMS": "0",
        "questions-MAX_NUM_FORMS": "3",
        "questions-0-id": question.pk,
        "questions-0-question": "Updated question?",
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
