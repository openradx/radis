import pytest
from adit_radis_shared.accounts.factories import UserFactory
from django.test import Client

from radis.labels.models import AnswerOption, Question, QuestionSet


def create_set():
    return QuestionSet.objects.create(name="Findings")


@pytest.mark.django_db
def test_question_set_list_requires_login(client: Client):
    response = client.get("/labels/")
    assert response.status_code == 302
    assert "/accounts/login/" in response["Location"]


@pytest.mark.django_db
def test_question_set_list_view(client: Client):
    user = UserFactory.create(is_active=True)
    create_set()
    client.force_login(user)
    response = client.get("/labels/")
    assert response.status_code == 200


@pytest.mark.django_db
def test_question_set_create_view(client: Client):
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
    assert QuestionSet.objects.filter(name="Protocols").exists()


@pytest.mark.django_db
def test_question_set_create_rejected_for_non_staff(client: Client):
    user = UserFactory.create(is_active=True, is_staff=False)
    client.force_login(user)
    response = client.post(
        "/labels/create/",
        {"name": "Should Fail", "is_active": True, "order": 1},
    )
    assert response.status_code == 403
    assert not QuestionSet.objects.filter(name="Should Fail").exists()


@pytest.mark.django_db
def test_question_create_view_with_default_options(client: Client):
    user = UserFactory.create(is_active=True, is_staff=True)
    question_set = create_set()
    client.force_login(user)

    response = client.post(
        f"/labels/{question_set.pk}/questions/create/",
        {
            "label": "Pulmonary embolism",
            "question": "Pulmonary embolism present?",
            "is_active": True,
            "order": 1,
        },
    )

    assert response.status_code == 302
    question = Question.objects.get(question_set=question_set, label="Pulmonary embolism")
    assert AnswerOption.objects.filter(question=question).count() == 3


@pytest.mark.django_db
def test_question_create_rejected_for_non_staff(client: Client):
    user = UserFactory.create(is_active=True, is_staff=False)
    question_set = create_set()
    client.force_login(user)

    response = client.post(
        f"/labels/{question_set.pk}/questions/create/",
        {"label": "Should Fail", "question": "Nope", "is_active": True, "order": 1},
    )
    assert response.status_code == 403
    assert not Question.objects.filter(label="Should Fail").exists()


@pytest.mark.django_db
def test_question_set_update_rejected_for_non_staff(client: Client):
    user = UserFactory.create(is_active=True, is_staff=False)
    question_set = create_set()
    client.force_login(user)

    response = client.post(
        f"/labels/{question_set.pk}/update/",
        {"name": "Hacked", "is_active": True, "order": 1},
    )
    assert response.status_code == 403
    question_set.refresh_from_db()
    assert question_set.name == "Findings"


@pytest.mark.django_db
def test_question_set_delete_rejected_for_non_staff(client: Client):
    user = UserFactory.create(is_active=True, is_staff=False)
    question_set = create_set()
    client.force_login(user)

    response = client.post(f"/labels/{question_set.pk}/delete/")
    assert response.status_code == 403
    assert QuestionSet.objects.filter(pk=question_set.pk).exists()
