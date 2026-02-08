import pytest
from adit_radis_shared.accounts.factories import UserFactory
from django.test import Client

from radis.labels.models import LabelChoice, LabelGroup, LabelQuestion


def create_group():
    return LabelGroup.objects.create(name="Findings", slug="findings")


@pytest.mark.django_db
def test_label_group_list_requires_login(client: Client):
    response = client.get("/labels/")
    assert response.status_code == 302
    assert "/accounts/login/" in response["Location"]


@pytest.mark.django_db
def test_label_group_list_view(client: Client):
    user = UserFactory.create(is_active=True)
    create_group()
    client.force_login(user)
    response = client.get("/labels/")
    assert response.status_code == 200


@pytest.mark.django_db
def test_label_group_create_view(client: Client):
    user = UserFactory.create(is_active=True)
    client.force_login(user)
    response = client.post(
        "/labels/create/",
        {
            "name": "Protocols",
            "slug": "protocols",
            "description": "Standard protocol labels",
            "is_active": True,
            "order": 1,
        },
    )
    assert response.status_code == 302
    assert LabelGroup.objects.filter(slug="protocols").exists()


@pytest.mark.django_db
def test_label_question_create_view_with_choices(client: Client):
    user = UserFactory.create(is_active=True)
    group = create_group()
    client.force_login(user)

    response = client.post(
        f"/labels/{group.pk}/questions/create/",
        {
            "name": "embolism",
            "question": "Pulmonary embolism present?",
            "description": "PE check",
            "is_active": True,
            "order": 1,
            "choices-TOTAL_FORMS": "3",
            "choices-INITIAL_FORMS": "0",
            "choices-MIN_NUM_FORMS": "1",
            "choices-MAX_NUM_FORMS": "1000",
            "choices-0-value": "yes",
            "choices-0-label": "Yes",
            "choices-0-is_unknown": "",
            "choices-0-order": "1",
            "choices-1-value": "no",
            "choices-1-label": "No",
            "choices-1-is_unknown": "",
            "choices-1-order": "2",
            "choices-2-value": "unknown",
            "choices-2-label": "Unknown",
            "choices-2-is_unknown": "on",
            "choices-2-order": "3",
        },
    )

    assert response.status_code == 302
    question = LabelQuestion.objects.get(group=group, name="embolism")
    assert LabelChoice.objects.filter(question=question).count() == 3
