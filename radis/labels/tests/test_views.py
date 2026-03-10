import pytest
from adit_radis_shared.accounts.factories import UserFactory
from django.test import Client

from radis.labels.models import LabelChoice, LabelGroup, LabelQuestion


def create_group():
    return LabelGroup.objects.create(name="Findings")


# -- List view (login required, no staff check) --


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


# -- Group create (staff required) --


@pytest.mark.django_db
def test_label_group_create_view(client: Client):
    user = UserFactory.create(is_active=True, is_staff=True)
    client.force_login(user)
    response = client.post(
        "/labels/create/",
        {
            "name": "Protocols",
            "description": "Standard protocol labels",
            "is_active": True,
            "order": 1,
        },
    )
    assert response.status_code == 302
    assert LabelGroup.objects.filter(name="Protocols").exists()


@pytest.mark.django_db
def test_label_group_create_rejected_for_non_staff(client: Client):
    user = UserFactory.create(is_active=True, is_staff=False)
    client.force_login(user)
    response = client.post(
        "/labels/create/",
        {"name": "Should Fail", "is_active": True, "order": 1},
    )
    assert response.status_code == 403
    assert not LabelGroup.objects.filter(name="Should Fail").exists()


# -- Question create (staff required) --


@pytest.mark.django_db
def test_label_question_create_view_with_choices(client: Client):
    user = UserFactory.create(is_active=True, is_staff=True)
    group = create_group()
    client.force_login(user)

    response = client.post(
        f"/labels/{group.pk}/questions/create/",
        {
            "label": "Pulmonary embolism",
            "question": "Pulmonary embolism present?",
            "is_active": True,
            "order": 1,
        },
    )

    assert response.status_code == 302
    question = LabelQuestion.objects.get(group=group, label="Pulmonary embolism")
    assert LabelChoice.objects.filter(question=question).count() == 3


@pytest.mark.django_db
def test_label_question_create_rejected_for_non_staff(client: Client):
    user = UserFactory.create(is_active=True, is_staff=False)
    group = create_group()
    client.force_login(user)

    response = client.post(
        f"/labels/{group.pk}/questions/create/",
        {"label": "Should Fail", "question": "Nope", "is_active": True, "order": 1},
    )
    assert response.status_code == 403
    assert not LabelQuestion.objects.filter(label="Should Fail").exists()


# -- Group update (staff required) --


@pytest.mark.django_db
def test_label_group_update_rejected_for_non_staff(client: Client):
    user = UserFactory.create(is_active=True, is_staff=False)
    group = create_group()
    client.force_login(user)

    response = client.post(
        f"/labels/{group.pk}/update/",
        {"name": "Hacked", "is_active": True, "order": 1},
    )
    assert response.status_code == 403
    group.refresh_from_db()
    assert group.name == "Findings"


# -- Group delete (staff required) --


@pytest.mark.django_db
def test_label_group_delete_rejected_for_non_staff(client: Client):
    user = UserFactory.create(is_active=True, is_staff=False)
    group = create_group()
    client.force_login(user)

    response = client.post(f"/labels/{group.pk}/delete/")
    assert response.status_code == 403
    assert LabelGroup.objects.filter(pk=group.pk).exists()
