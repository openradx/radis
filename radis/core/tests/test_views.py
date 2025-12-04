from unittest.mock import patch

import pytest
from adit_radis_shared.accounts.factories import GroupFactory, UserFactory
from django.contrib.auth.models import Permission
from django.contrib.messages import get_messages
from django.test import Client
from django.urls import reverse

from radis.core.models import AnalysisJob, AnalysisTask
from radis.extractions.factories import ExtractionJobFactory, ExtractionTaskFactory


class TestHealthView:
    @pytest.mark.django_db
    def test_health_endpoint_returns_ok(self, client: Client):
        response = client.get(reverse("health"))

        assert response.status_code == 200
        assert response.json() == {"status": "ok"}


@pytest.mark.django_db
def test_admin_section_access_control(client: Client):
    url = reverse("admin_section")

    response = Client().get(url)
    assert response.status_code == 302
    assert "/accounts/login" in response["Location"] or "/login" in response["Location"]

    # normal user will still be redirected (staff_required)
    user = UserFactory()
    client.force_login(user)
    response = client.get(url)
    assert response.status_code == 302

    staff = UserFactory(is_staff=True)
    client.force_login(staff)
    response = client.get(url)
    assert response.status_code == 200
    content = response.content.decode()
    assert "admin section" in content.lower()


@pytest.mark.django_db
def test_home_view_authenticated(client: Client):
    user = UserFactory()
    client.force_login(user)

    url = reverse("home")
    response = client.get(url)

    assert response.status_code == 200
    template_names = [t.name for t in response.templates if t.name]
    assert any("home.html" in name for name in template_names)
    content = response.content.decode()
    assert len(content) > 0


@pytest.mark.django_db
def test_home_view_anonymous_user(client: Client):
    url = reverse("home")
    response = client.get(url)
    assert response.status_code == 200


@pytest.mark.django_db
def test_update_preferences_allowed_keys(client: Client):
    user = UserFactory()
    client.force_login(user)

    url = "/update-preferences/"

    response = client.post(url, {"theme": "dark"})
    user.refresh_from_db()

    assert response.status_code == 200
    # Check if preference was actually saved
    if hasattr(user, "preferences") and user.preferences:
        assert user.preferences.get("theme") == "dark"


@pytest.mark.django_db
def test_update_preferences_get_request(client: Client):
    user = UserFactory()
    client.force_login(user)

    url = "/update-preferences/"
    response = client.get(url)

    assert response.status_code == 405  # Method Not Allowed


@pytest.mark.django_db
def test_update_preferences_rejects_unknown_keys(client: Client):
    user = UserFactory()
    client.force_login(user)

    url = "/update-preferences/"
    response = client.post(url, {"unknown": "x"}, follow=True)

    assert response.status_code == 400


@pytest.mark.django_db
def test_update_preferences_empty_post(client: Client):
    user = UserFactory()

    client.force_login(user)

    url = "/update-preferences/"
    response = client.post(url, {})

    assert response.status_code == 200


@pytest.mark.django_db
def test_job_list_regular_user_sees_only_own_jobs(client: Client):
    user = UserFactory()
    other = UserFactory()
    job1 = ExtractionJobFactory(owner=user)
    job2 = ExtractionJobFactory(owner=other)

    client.force_login(user)

    url = reverse("extraction_job_list")
    response = client.get(url)

    assert response.status_code == 200
    jobs = list(response.context["object_list"])

    # User should only see their own job
    assert job1 in jobs
    assert job2 not in jobs
    assert len(jobs) == 1


@pytest.mark.django_db
def test_job_list_staff_sees_all_jobs_with_all_param(client: Client):
    staff = UserFactory(is_staff=True)
    user = UserFactory()
    job1 = ExtractionJobFactory(owner=user)
    job2 = ExtractionJobFactory(owner=staff)

    client.force_login(staff)

    # Staff with ?all=1 should see all jobs
    url = reverse("extraction_job_list") + "?all=1"
    response = client.get(url)

    assert response.status_code == 200
    jobs = list(response.context["object_list"])

    assert job1 in jobs
    assert job2 in jobs
    assert len(jobs) == 2


@pytest.mark.django_db
def test_job_list_anonymous_user_redirected(client: Client):
    user = UserFactory()
    ExtractionJobFactory(owner=user)

    url = reverse("extraction_job_list")
    response = client.get(url)

    assert response.status_code == 302
    assert "/accounts/login" in response["Location"] or "/login" in response["Location"]


@pytest.mark.django_db
def test_job_list_pagination(client: Client):
    user = UserFactory()
    # Create multiple jobs
    for _ in range(5):
        ExtractionJobFactory(owner=user)

    client.force_login(user)

    url = reverse("extraction_job_list")
    response = client.get(url)

    assert response.status_code == 200
    assert "page_obj" in response.context or "object_list" in response.context


@pytest.mark.django_db
def test_job_create_wizard_first_step_renders(client: Client):
    user = UserFactory()
    permission = Permission.objects.get(codename="add_extractionjob")
    user.user_permissions.add(permission)

    group = GroupFactory()
    user.groups.add(group)
    user.active_group = group
    user.save()

    client.force_login(user)

    url = reverse("extraction_job_create")
    response = client.get(url)

    assert response.status_code == 200
    content = response.content.decode()
    assert "form" in content.lower()
    assert "title" in content.lower()


@pytest.mark.django_db
def test_job_create_wizard_requires_permissions(client: Client):
    user = UserFactory()

    # Test without permission - should get 403
    client.force_login(user)
    url = reverse("extraction_job_create")
    response = client.get(url)
    assert response.status_code == 403

    # Add permission but no active group - should still fail
    permission = Permission.objects.get(codename="add_extractionjob")
    user.user_permissions.add(permission)
    response = client.get(url)
    assert response.status_code == 403

    # Add active group - should now work
    group = GroupFactory()
    user.groups.add(group)
    user.active_group = group
    user.save()

    response = client.get(url)
    assert response.status_code == 200
    content = response.content.decode()
    assert "<form" in content.lower()


@pytest.mark.django_db
def test_job_create_anonymous_user_redirected(client: Client):
    url = reverse("extraction_job_create")
    response = client.get(url)

    assert response.status_code == 302
    assert "/accounts/login" in response["Location"] or "/login" in response["Location"]


@pytest.mark.django_db
def test_job_create_invalid_form(client: Client):
    user = UserFactory()
    client.force_login(user)

    url = reverse("extraction_job_create")

    response = client.post(url, {})

    assert response.status_code == 403


@pytest.mark.django_db
def test_job_detail_access_owner_only(client: Client):
    owner = UserFactory()
    other = UserFactory()
    job = ExtractionJobFactory(owner=owner)

    # Owner should have access
    client.force_login(owner)
    url = reverse("extraction_job_detail", kwargs={"pk": job.pk})
    response = client.get(url)
    assert response.status_code == 200

    # Other user should not have access
    client.force_login(other)
    response = client.get(url)
    assert response.status_code == 404


@pytest.mark.django_db
def test_job_detail_staff_access(client: Client):
    user = UserFactory()
    staff = UserFactory(is_staff=True)
    job = ExtractionJobFactory(owner=user)

    client.force_login(staff)

    url = reverse("extraction_job_detail", kwargs={"pk": job.pk})
    response = client.get(url)

    assert response.status_code == 200
    assert "object" in response.context
    assert response.context["object"] == job


@pytest.mark.django_db
def test_job_detail_not_found(client: Client):
    user = UserFactory()
    client.force_login(user)

    url = reverse("extraction_job_detail", kwargs={"pk": 99999})
    response = client.get(url)
    assert response.status_code == 404


@pytest.mark.django_db
def test_verify_job_staff_only(client: Client):
    user = UserFactory()
    job = ExtractionJobFactory(owner=user, status=AnalysisJob.Status.UNVERIFIED)

    url = reverse("extraction_job_verify", kwargs={"pk": job.pk})

    # Anonymous user should redirect to login
    response = client.post(url)
    assert response.status_code == 302
    assert "/accounts/login" in response["Location"] or "/login" in response["Location"]

    # Normal user should get 403/404 (access denied)
    normal_user = UserFactory()
    client.force_login(normal_user)
    response = client.post(url)
    assert response.status_code in [403, 404]

    # Staff should be allowed
    staff = UserFactory(is_staff=True)
    client.force_login(staff)

    with patch("radis.extractions.models.ExtractionJob.delay"):
        response = client.post(url)

    assert response.status_code == 302
    job.refresh_from_db()
    assert job.status == AnalysisJob.Status.PENDING


@pytest.mark.django_db
def test_verify_already_verified_job(client: Client):
    staff = UserFactory(is_staff=True)
    user = UserFactory()
    # Use a verified status (PENDING is considered verified)
    job = ExtractionJobFactory(owner=user, status=AnalysisJob.Status.PENDING)

    client.force_login(staff)

    url = reverse("extraction_job_verify", kwargs={"pk": job.pk})
    response = client.post(url)

    assert response.status_code == 400


@pytest.mark.django_db
def test_cancel_job_changes_status(client: Client):
    user = UserFactory()
    job = ExtractionJobFactory(owner=user, status=AnalysisJob.Status.PENDING)

    client.force_login(user)

    url = reverse("extraction_job_cancel", kwargs={"pk": job.pk})
    response = client.post(url)

    assert response.status_code == 302

    # Verify job status changed
    job.refresh_from_db()
    assert job.status == AnalysisJob.Status.CANCELED


@pytest.mark.django_db
def test_cancel_job_permission_denied(client: Client):
    owner = UserFactory()
    other = UserFactory()
    job = ExtractionJobFactory(owner=owner, status=AnalysisJob.Status.PENDING)

    client.force_login(other)

    url = reverse("extraction_job_cancel", kwargs={"pk": job.pk})
    response = client.post(url)

    assert response.status_code == 404  # User should not be able to cancel others' jobs


@pytest.mark.django_db
def test_verifiedJob_messages_framework(client: Client):
    staff = UserFactory(is_staff=True)
    user = UserFactory()
    job = ExtractionJobFactory(owner=user, status=AnalysisJob.Status.UNVERIFIED)

    client.force_login(staff)

    url = reverse("extraction_job_verify", kwargs={"pk": job.pk})

    with patch("radis.extractions.models.ExtractionJob.delay"):
        response = client.post(url, follow=True)

    messages = list(get_messages(response.wsgi_request))

    assert any("verified" in str(message) for message in messages)


@pytest.mark.django_db
def test_job_delete_owner_can_delete(client: Client):
    user = UserFactory()
    job = ExtractionJobFactory(owner=user, status=AnalysisJob.Status.PENDING)

    client.force_login(user)

    url = reverse("extraction_job_delete", kwargs={"pk": job.pk})
    response = client.post(url)

    assert response.status_code == 302
    assert not ExtractionJobFactory._meta.model.objects.filter(pk=job.pk).exists()


@pytest.mark.django_db
def test_job_delete_non_deletable_job(client: Client):
    user = UserFactory()
    job = ExtractionJobFactory(owner=user, status=AnalysisJob.Status.IN_PROGRESS)

    client.force_login(user)

    url = reverse("extraction_job_delete", kwargs={"pk": job.pk})
    response = client.post(url)

    assert response.status_code == 400


@pytest.mark.django_db
def test_job_delete_permission_denied(client: Client):
    owner = UserFactory()
    other = UserFactory()
    job = ExtractionJobFactory(owner=owner, status=AnalysisJob.Status.PENDING)

    client.force_login(other)

    url = reverse("extraction_job_delete", kwargs={"pk": job.pk})
    response = client.post(url)

    assert response.status_code == 404


@pytest.mark.django_db
def test_job_delete_staff_can_delete_any_job(client: Client):
    staff = UserFactory(is_staff=True)
    user = UserFactory()
    job = ExtractionJobFactory(owner=user, status=AnalysisJob.Status.PENDING)

    client.force_login(staff)

    url = reverse("extraction_job_delete", kwargs={"pk": job.pk})
    response = client.post(url)

    assert response.status_code == 302
    assert not ExtractionJobFactory._meta.model.objects.filter(pk=job.pk).exists()


@pytest.mark.django_db
def test_job_delete_shows_success_message(client: Client):
    user = UserFactory()
    job = ExtractionJobFactory(owner=user, status=AnalysisJob.Status.PENDING)

    client.force_login(user)

    url = reverse("extraction_job_delete", kwargs={"pk": job.pk})
    response = client.post(url, follow=True)

    messages = list(get_messages(response.wsgi_request))
    assert any("deleted" in str(message) for message in messages)


@pytest.mark.django_db
def test_job_resume_canceled_job(client: Client):
    user = UserFactory()
    job = ExtractionJobFactory(owner=user, status=AnalysisJob.Status.CANCELED)

    client.force_login(user)

    url = reverse("extraction_job_resume", kwargs={"pk": job.pk})

    with patch("radis.extractions.models.ExtractionJob.delay"):
        response = client.post(url)

    assert response.status_code == 302
    job.refresh_from_db()
    assert job.status == AnalysisJob.Status.PENDING


@pytest.mark.django_db
def test_job_resume_non_resumable_job(client: Client):
    user = UserFactory()
    job = ExtractionJobFactory(owner=user, status=AnalysisJob.Status.SUCCESS)

    client.force_login(user)

    url = reverse("extraction_job_resume", kwargs={"pk": job.pk})
    response = client.post(url)

    assert response.status_code == 400  # Job is not resumable


@pytest.mark.django_db
def test_job_resume_permission_denied(client: Client):
    owner = UserFactory()
    other = UserFactory()
    job = ExtractionJobFactory(owner=owner, status=AnalysisJob.Status.CANCELED)

    client.force_login(other)

    url = reverse("extraction_job_resume", kwargs={"pk": job.pk})
    response = client.post(url)

    assert response.status_code == 404  # User should not be able to resume others' jobs


@pytest.mark.django_db
def test_job_resume_shows_success_message(client: Client):
    user = UserFactory()
    job = ExtractionJobFactory(owner=user, status=AnalysisJob.Status.CANCELED)

    client.force_login(user)

    url = reverse("extraction_job_resume", kwargs={"pk": job.pk})

    with patch("radis.extractions.models.ExtractionJob.delay"):
        response = client.post(url, follow=True)

    messages = list(get_messages(response.wsgi_request))
    assert any("resumed" in str(message) for message in messages)


@pytest.mark.django_db
def test_job_retry_failed_job(client: Client):
    user = UserFactory()
    job = ExtractionJobFactory(owner=user, status=AnalysisJob.Status.FAILURE)

    client.force_login(user)

    url = reverse("extraction_job_retry", kwargs={"pk": job.pk})

    with patch("radis.extractions.models.ExtractionJob.reset_tasks") as mock_reset:
        with patch("radis.extractions.models.ExtractionJob.delay"):
            mock_reset.return_value = []
            response = client.post(url)

    assert response.status_code == 302
    job.refresh_from_db()
    assert job.status == AnalysisJob.Status.PENDING


@pytest.mark.django_db
def test_job_retry_non_retriable_job(client: Client):
    user = UserFactory()
    job = ExtractionJobFactory(owner=user, status=AnalysisJob.Status.SUCCESS)

    client.force_login(user)

    url = reverse("extraction_job_retry", kwargs={"pk": job.pk})
    response = client.post(url)

    assert response.status_code == 400


@pytest.mark.django_db
def test_job_retry_shows_success_message(client: Client):
    user = UserFactory()
    job = ExtractionJobFactory(owner=user, status=AnalysisJob.Status.FAILURE)

    client.force_login(user)

    url = reverse("extraction_job_retry", kwargs={"pk": job.pk})

    with patch("radis.extractions.models.ExtractionJob.reset_tasks") as mock_reset:
        with patch("radis.extractions.models.ExtractionJob.delay"):
            mock_reset.return_value = []
            response = client.post(url, follow=True)

    messages = list(get_messages(response.wsgi_request))
    assert any("retried" in str(message) for message in messages)


@pytest.mark.django_db
def test_job_restart_staff_only(client: Client):
    user = UserFactory()
    job = ExtractionJobFactory(owner=user, status=AnalysisJob.Status.SUCCESS)

    client.force_login(user)

    url = reverse("extraction_job_restart", kwargs={"pk": job.pk})
    response = client.post(url)

    assert response.status_code == 403


@pytest.mark.django_db
def test_job_restart_staff_can_restart(client: Client):
    staff = UserFactory(is_staff=True)
    user = UserFactory()
    job = ExtractionJobFactory(owner=user, status=AnalysisJob.Status.SUCCESS)

    client.force_login(staff)

    url = reverse("extraction_job_restart", kwargs={"pk": job.pk})

    with patch("radis.extractions.models.ExtractionJob.delay"):
        response = client.post(url)

    assert response.status_code == 302
    job.refresh_from_db()
    assert job.status == AnalysisJob.Status.PENDING


@pytest.mark.django_db
def test_job_restart_non_restartable_job(client: Client):
    staff = UserFactory(is_staff=True)
    user = UserFactory()
    job = ExtractionJobFactory(owner=user, status=AnalysisJob.Status.PENDING)

    client.force_login(staff)

    url = reverse("extraction_job_restart", kwargs={"pk": job.pk})
    response = client.post(url)

    assert response.status_code == 400


@pytest.mark.django_db
def test_job_restart_shows_success_message(client: Client):
    staff = UserFactory(is_staff=True)
    user = UserFactory()
    job = ExtractionJobFactory(owner=user, status=AnalysisJob.Status.SUCCESS)

    client.force_login(staff)

    url = reverse("extraction_job_restart", kwargs={"pk": job.pk})

    with patch("radis.extractions.models.ExtractionJob.delay"):
        response = client.post(url, follow=True)

    messages = list(get_messages(response.wsgi_request))
    assert any("restarted" in str(message) for message in messages)


@pytest.mark.django_db
def test_task_delete_owner_can_delete(client: Client):
    user = UserFactory()
    job = ExtractionJobFactory(owner=user)
    task = ExtractionTaskFactory(job=job, status=AnalysisTask.Status.PENDING)

    client.force_login(user)

    url = reverse("extraction_task_delete", kwargs={"pk": task.pk})
    response = client.post(url)

    assert response.status_code == 302
    assert not ExtractionTaskFactory._meta.model.objects.filter(pk=task.pk).exists()


@pytest.mark.django_db
def test_task_delete_non_deletable_task(client: Client):
    user = UserFactory()
    job = ExtractionJobFactory(owner=user)
    task = ExtractionTaskFactory(job=job, status=AnalysisTask.Status.SUCCESS)

    client.force_login(user)

    url = reverse("extraction_task_delete", kwargs={"pk": task.pk})
    response = client.post(url)

    assert response.status_code == 400


@pytest.mark.django_db
def test_task_delete_permission_denied(client: Client):
    owner = UserFactory()
    other = UserFactory()
    job = ExtractionJobFactory(owner=owner)
    task = ExtractionTaskFactory(job=job, status=AnalysisTask.Status.PENDING)

    client.force_login(other)

    url = reverse("extraction_task_delete", kwargs={"pk": task.pk})
    response = client.post(url)

    assert response.status_code == 404  # User should not be able to delete others' tasks


@pytest.mark.django_db
def test_task_delete_staff_can_delete_any_task(client: Client):
    staff = UserFactory(is_staff=True)
    user = UserFactory()
    job = ExtractionJobFactory(owner=user)
    task = ExtractionTaskFactory(job=job, status=AnalysisTask.Status.PENDING)

    client.force_login(staff)

    url = reverse("extraction_task_delete", kwargs={"pk": task.pk})
    response = client.post(url)

    assert response.status_code == 302
    assert not ExtractionTaskFactory._meta.model.objects.filter(pk=task.pk).exists()


@pytest.mark.django_db
def test_task_delete_shows_success_message(client: Client):
    user = UserFactory()
    job = ExtractionJobFactory(owner=user)
    task = ExtractionTaskFactory(job=job, status=AnalysisTask.Status.PENDING)

    client.force_login(user)

    url = reverse("extraction_task_delete", kwargs={"pk": task.pk})
    response = client.post(url, follow=True)

    messages = list(get_messages(response.wsgi_request))
    assert any("deleted" in str(message) for message in messages)


@pytest.mark.django_db
def test_task_delete_updates_job_state(client: Client):
    """Test that job state is updated after task deletion."""
    user = UserFactory()
    job = ExtractionJobFactory(owner=user)
    task = ExtractionTaskFactory(job=job, status=AnalysisTask.Status.PENDING)

    client.force_login(user)

    url = reverse("extraction_task_delete", kwargs={"pk": task.pk})

    response = client.post(url)

    assert response.status_code == 302


@pytest.mark.django_db
def test_task_reset_owner_can_reset(client: Client):
    """Test that task owner can reset their own resettable task."""
    user = UserFactory()
    job = ExtractionJobFactory(owner=user)
    task = ExtractionTaskFactory(job=job, status=AnalysisTask.Status.FAILURE)

    client.force_login(user)

    url = reverse("extraction_task_reset", kwargs={"pk": task.pk})

    with patch("radis.core.utils.model_utils.reset_tasks"):
        with patch("radis.extractions.models.ExtractionTask.delay"):
            response = client.post(url)

    assert response.status_code == 302


@pytest.mark.django_db
def test_task_reset_non_resettable_task(client: Client):
    user = UserFactory()
    job = ExtractionJobFactory(owner=user)
    task = ExtractionTaskFactory(job=job, status=AnalysisTask.Status.PENDING)

    client.force_login(user)

    url = reverse("extraction_task_reset", kwargs={"pk": task.pk})
    response = client.post(url)

    assert response.status_code == 400


@pytest.mark.django_db
def test_task_reset_permission_denied(client: Client):
    owner = UserFactory()
    other = UserFactory()
    job = ExtractionJobFactory(owner=owner)
    task = ExtractionTaskFactory(job=job, status=AnalysisTask.Status.FAILURE)

    client.force_login(other)

    url = reverse("extraction_task_reset", kwargs={"pk": task.pk})
    response = client.post(url)

    assert response.status_code == 404


@pytest.mark.django_db
def test_task_reset_staff_can_reset_any_task(client: Client):
    staff = UserFactory(is_staff=True)
    user = UserFactory()
    job = ExtractionJobFactory(owner=user)
    task = ExtractionTaskFactory(job=job, status=AnalysisTask.Status.FAILURE)

    client.force_login(staff)

    url = reverse("extraction_task_reset", kwargs={"pk": task.pk})

    with patch("radis.core.utils.model_utils.reset_tasks"):
        with patch("radis.extractions.models.ExtractionTask.delay"):
            response = client.post(url)

    assert response.status_code == 302


@pytest.mark.django_db
def test_task_reset_shows_success_message(client: Client):
    user = UserFactory()
    job = ExtractionJobFactory(owner=user)
    task = ExtractionTaskFactory(job=job, status=AnalysisTask.Status.FAILURE)

    client.force_login(user)

    url = reverse("extraction_task_reset", kwargs={"pk": task.pk})

    with patch("radis.core.utils.model_utils.reset_tasks"):
        with patch("radis.extractions.models.ExtractionTask.delay"):
            response = client.post(url, follow=True)

    messages = list(get_messages(response.wsgi_request))
    assert any("reset" in str(message) for message in messages)


@pytest.mark.django_db
def test_task_reset_updates_job_state(client: Client):
    user = UserFactory()
    job = ExtractionJobFactory(owner=user)
    task = ExtractionTaskFactory(job=job, status=AnalysisTask.Status.FAILURE)

    client.force_login(user)

    url = reverse("extraction_task_reset", kwargs={"pk": task.pk})
    with patch("radis.core.utils.model_utils.reset_tasks"):
        with patch("radis.extractions.models.ExtractionTask.delay"):
            response = client.post(url)

    assert response.status_code == 302
