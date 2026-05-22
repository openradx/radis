import pytest
from django.contrib.auth import get_user_model
from django.urls import reverse

from radis.labels.factories import AnswerFactory, QuestionFactory


User = get_user_model()


@pytest.fixture
def admin_client(client):
    user = User.objects.create_superuser(
        username="admin", email="admin@example.com", password="pw"
    )
    client.force_login(user)
    return client


class TestQuestionAdmin:
    def test_changelist_loads(self, admin_client):
        QuestionFactory(label="pneumonia")
        resp = admin_client.get(reverse("admin:labels_question_changelist"))
        assert resp.status_code == 200
        assert b"pneumonia" in resp.content

    def test_add_form_loads(self, admin_client):
        resp = admin_client.get(reverse("admin:labels_question_add"))
        assert resp.status_code == 200

    def test_duplicate_label_rejected(self, admin_client):
        QuestionFactory(label="pneumonia")
        resp = admin_client.post(
            reverse("admin:labels_question_add"),
            data={"label": "pneumonia", "text": "q?", "group": "g", "active": "on"},
        )
        assert resp.status_code == 200
        assert b"already exists" in resp.content.lower() or b"unique" in resp.content.lower()


class TestAnswerAdmin:
    def test_changelist_loads(self, admin_client):
        AnswerFactory()
        resp = admin_client.get(reverse("admin:labels_answer_changelist"))
        assert resp.status_code == 200

    def test_add_disabled(self, admin_client):
        resp = admin_client.get(reverse("admin:labels_answer_add"))
        assert resp.status_code in (302, 403)


from radis.reports.factories import ReportFactory


def test_report_change_shows_answer_inline(admin_client):
    r = ReportFactory()
    AnswerFactory(report=r)
    resp = admin_client.get(reverse("admin:reports_report_change", args=[r.id]))
    assert resp.status_code == 200
    assert b"answer" in resp.content.lower()
