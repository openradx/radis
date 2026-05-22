from unittest.mock import patch

import pytest
from adit_radis_shared.accounts.factories import AdminUserFactory
from django.urls import reverse

from radis.core.models import AnalysisJob
from radis.labels.factories import AnswerFactory, LabelingJobFactory, QuestionFactory
from radis.reports.factories import ReportFactory


@pytest.fixture
def admin_client(client):
    user = AdminUserFactory.create()
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

    def test_question_add_with_datalist_widget_works(self, admin_client):
        resp = admin_client.post(
            reverse("admin:labels_question_add"),
            data={"label": "pneu", "text": "Q?", "group": "lung", "active": "on"},
        )
        # Successful add redirects (302) to the changelist.
        assert resp.status_code in (200, 302)
        if resp.status_code == 200:
            # If the form re-rendered, there should be no validation errors.
            assert b"errorlist" not in resp.content


class TestAnswerAdmin:
    def test_changelist_loads(self, admin_client):
        AnswerFactory()
        resp = admin_client.get(reverse("admin:labels_answer_changelist"))
        assert resp.status_code == 200

    def test_add_disabled(self, admin_client):
        resp = admin_client.get(reverse("admin:labels_answer_add"))
        assert resp.status_code in (302, 403)


def test_report_change_shows_answer_inline(admin_client):
    r = ReportFactory()
    AnswerFactory(report=r)
    resp = admin_client.get(reverse("admin:reports_report_change", args=[r.id]))
    assert resp.status_code == 200
    assert b"answer" in resp.content.lower()


class TestLabelingJobAdmin:
    def test_changelist_loads(self, admin_client):
        LabelingJobFactory(status=AnalysisJob.Status.PENDING)
        resp = admin_client.get(reverse("admin:labels_labelingjob_changelist"))
        assert resp.status_code == 200

    def test_add_disabled(self, admin_client):
        resp = admin_client.get(reverse("admin:labels_labelingjob_add"))
        assert resp.status_code in (302, 403)


class TestRunCancelBackfill:
    def test_run_creates_job_when_none_active(self, admin_client):
        with patch("radis.labels.admin_views.LabelingJob.delay"):
            resp = admin_client.post(reverse("admin:labels_run_backfill"))
        assert resp.status_code in (302, 303)
        from radis.labels.models import LabelingJob

        assert LabelingJob.objects.count() == 1

    def test_run_rejected_when_one_active(self, admin_client):
        LabelingJobFactory(status=AnalysisJob.Status.PENDING)
        resp = admin_client.post(reverse("admin:labels_run_backfill"), follow=True)
        assert resp.status_code == 200
        assert b"another" in resp.content.lower() or b"already active" in resp.content.lower()

    def test_cancel_sets_canceling(self, admin_client):
        job = LabelingJobFactory(status=AnalysisJob.Status.IN_PROGRESS)
        resp = admin_client.post(reverse("admin:labels_cancel_backfill", args=[job.id]))
        assert resp.status_code in (302, 303)
        job.refresh_from_db()
        assert job.status == AnalysisJob.Status.CANCELING
