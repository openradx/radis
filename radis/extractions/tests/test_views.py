import csv
import io

import pytest
from adit_radis_shared.accounts.factories import GroupFactory, UserFactory
from django.contrib.auth.models import Permission
from django.test import Client

from radis.core.models import AnalysisTask
from radis.extractions.factories import (
    ExtractionInstanceFactory,
    ExtractionJobFactory,
    ExtractionTaskFactory,
    OutputFieldFactory,
)
from radis.extractions.models import ExtractionJob
from radis.reports.factories import LanguageFactory, ReportFactory


def create_test_extraction_job(owner=None, group=None):
    if not owner:
        owner = UserFactory.create(is_active=True)
    if not group:
        group = GroupFactory.create()
    language = LanguageFactory.create(code="en")
    return ExtractionJobFactory.create(owner=owner, language=language, group=group)


def create_test_extraction_task(job=None):
    if not job:
        owner = UserFactory.create(is_active=True)
        job = create_test_extraction_job(owner=owner)
    return ExtractionTaskFactory.create(job=job)


@pytest.fixture(autouse=True)
def disable_debug_toolbar(settings):
    settings.MIDDLEWARE = [
        middleware
        for middleware in settings.MIDDLEWARE
        if middleware != "debug_toolbar.middleware.DebugToolbarMiddleware"
    ]


@pytest.mark.django_db
def test_extraction_job_list_view(client: Client):
    user = UserFactory.create(is_active=True)
    client.force_login(user)
    response = client.get("/extractions/jobs/")
    assert response.status_code == 200


@pytest.mark.django_db
def test_extraction_job_create_view_get(client: Client):
    user = UserFactory.create(is_active=True, is_staff=True)
    group = GroupFactory.create()
    user.groups.add(group)
    user.active_group = group
    user.save()

    permission = Permission.objects.get(codename="add_extractionjob")
    user.user_permissions.add(permission)

    client.force_login(user)
    response = client.get("/extractions/jobs/new/")
    assert response.status_code == 200


@pytest.mark.django_db
def test_extraction_job_detail_view(client: Client):
    user = UserFactory.create(is_active=True)
    job = create_test_extraction_job(owner=user)
    client.force_login(user)
    response = client.get(f"/extractions/jobs/{job.pk}/")
    assert response.status_code == 200


@pytest.mark.django_db
def test_extraction_job_detail_view_unauthorized(client: Client):
    user = UserFactory.create(is_active=True)
    other_user = UserFactory.create(is_active=True)
    job = create_test_extraction_job(owner=other_user)
    client.force_login(user)
    response = client.get(f"/extractions/jobs/{job.pk}/")
    assert response.status_code == 404  # Unauthorized access should return 404


@pytest.mark.django_db
def test_extraction_job_delete_view_post(client: Client):
    user = UserFactory.create(is_active=True)
    job = create_test_extraction_job(owner=user)
    client.force_login(user)
    response = client.post(f"/extractions/jobs/{job.pk}/delete/")
    assert response.status_code == 302


@pytest.mark.django_db
def test_extraction_job_delete_unauthorized(client: Client):
    user = UserFactory.create(is_active=True)
    other_user = UserFactory.create(is_active=True)
    job = create_test_extraction_job(owner=other_user)
    client.force_login(user)
    response = client.post(f"/extractions/jobs/{job.pk}/delete/")
    assert response.status_code == 404  # Unauthorized access should return 404


@pytest.mark.django_db
def test_extraction_job_verify_view(client: Client):
    user = UserFactory.create(is_active=True, is_staff=True)
    job = create_test_extraction_job(owner=user)
    client.force_login(user)
    response = client.post(f"/extractions/jobs/{job.pk}/verify/")
    assert response.status_code == 302


@pytest.mark.django_db
def test_extraction_job_verify_unauthorized(client: Client):
    user = UserFactory.create(is_active=True, is_staff=False)
    job = create_test_extraction_job(owner=user)
    client.force_login(user)
    response = client.post(f"/extractions/jobs/{job.pk}/verify/")
    assert response.status_code == 403  # Non-staff users should get 403 Forbidden


@pytest.mark.django_db
def test_extraction_job_cancel_view_success(client: Client):
    user = UserFactory.create(is_active=True)
    job = create_test_extraction_job(owner=user)
    job.status = ExtractionJob.Status.PENDING
    job.save()
    client.force_login(user)
    response = client.post(f"/extractions/jobs/{job.pk}/cancel/")
    assert response.status_code == 302


@pytest.mark.django_db
def test_extraction_job_cancel_view_invalid_status(client: Client):
    user = UserFactory.create(is_active=True)
    job = create_test_extraction_job(owner=user)
    # Job starts with UNVERIFIED status, which is not cancelable
    client.force_login(user)
    response = client.post(f"/extractions/jobs/{job.pk}/cancel/")
    assert response.status_code == 400


@pytest.mark.django_db
def test_extraction_job_resume_view_success(client: Client):
    user = UserFactory.create(is_active=True)
    job = create_test_extraction_job(owner=user)
    job.status = ExtractionJob.Status.CANCELED
    job.save()
    client.force_login(user)
    response = client.post(f"/extractions/jobs/{job.pk}/resume/")
    assert response.status_code == 302


@pytest.mark.django_db
def test_extraction_job_resume_view_invalid_status(client: Client):
    user = UserFactory.create(is_active=True)
    job = create_test_extraction_job(owner=user)
    # Job starts with UNVERIFIED status, which is not resumable
    client.force_login(user)
    response = client.post(f"/extractions/jobs/{job.pk}/resume/")
    assert response.status_code == 400


@pytest.mark.django_db
def test_extraction_job_retry_view_success(client: Client):
    user = UserFactory.create(is_active=True)
    job = create_test_extraction_job(owner=user)
    job.status = ExtractionJob.Status.FAILURE
    job.save()
    client.force_login(user)
    response = client.post(f"/extractions/jobs/{job.pk}/retry/")
    assert response.status_code == 302


@pytest.mark.django_db
def test_extraction_job_retry_view_invalid_status(client: Client):
    user = UserFactory.create(is_active=True)
    job = create_test_extraction_job(owner=user)
    # Job starts with UNVERIFIED status, which is not retriable
    client.force_login(user)
    response = client.post(f"/extractions/jobs/{job.pk}/retry/")
    assert response.status_code == 400


@pytest.mark.django_db
def test_extraction_job_restart_view_success(client: Client):
    user = UserFactory.create(is_active=True, is_staff=True)
    job = create_test_extraction_job(owner=user)
    job.status = ExtractionJob.Status.SUCCESS
    job.save()
    client.force_login(user)
    response = client.post(f"/extractions/jobs/{job.pk}/restart/")
    assert response.status_code == 302


@pytest.mark.django_db
def test_extraction_job_restart_view_invalid_status(client: Client):
    user = UserFactory.create(is_active=True, is_staff=True)
    job = create_test_extraction_job(owner=user)
    # Job starts with UNVERIFIED status, which is not restartable
    client.force_login(user)
    response = client.post(f"/extractions/jobs/{job.pk}/restart/")
    assert response.status_code == 400


@pytest.mark.django_db
def test_extraction_result_list_view(client: Client):
    user = UserFactory.create(is_active=True)
    job = create_test_extraction_job(owner=user)
    client.force_login(user)
    response = client.get(f"/extractions/jobs/{job.pk}/results/")
    assert response.status_code == 200
    assert "Download CSV" not in response.content.decode()


@pytest.mark.django_db
def test_extraction_task_detail_view(client: Client):
    user = UserFactory.create(is_active=True)
    job = create_test_extraction_job(owner=user)
    task = create_test_extraction_task(job=job)
    client.force_login(user)
    response = client.get(f"/extractions/tasks/{task.pk}/")
    assert response.status_code == 200


@pytest.mark.django_db
def test_extraction_task_detail_unauthorized(client: Client):
    user = UserFactory.create(is_active=True)
    other_user = UserFactory.create(is_active=True)
    job = create_test_extraction_job(owner=other_user)
    task = create_test_extraction_task(job=job)
    client.force_login(user)
    response = client.get(f"/extractions/tasks/{task.pk}/")
    assert response.status_code == 404


@pytest.mark.django_db
def test_extraction_task_delete_view(client: Client):
    user = UserFactory.create(is_active=True)
    job = create_test_extraction_job(owner=user)
    task = create_test_extraction_task(job=job)
    task.status = AnalysisTask.Status.PENDING
    task.save()
    client.force_login(user)
    response = client.post(f"/extractions/tasks/{task.pk}/delete/")
    assert response.status_code == 302


@pytest.mark.django_db
def test_extraction_task_reset_view(client: Client):
    user = UserFactory.create(is_active=True)
    job = create_test_extraction_job(
        owner=user,
    )
    task = create_test_extraction_task(job=job)
    task.status = AnalysisTask.Status.FAILURE
    task.save()
    client.force_login(user)
    response = client.post(f"/extractions/tasks/{task.pk}/reset/")
    assert response.status_code == 302


@pytest.mark.django_db
def test_extraction_update_preferences_view(client: Client):
    user = UserFactory.create(is_active=True)
    client.force_login(user)
    response = client.post(
        "/extractions/update-preferences/",
        {"extractions_search_provider": "pg_search"},
    )
    assert response.status_code == 200


@pytest.mark.django_db
def test_extraction_help_view(client: Client):
    user = UserFactory.create(is_active=True)
    client.force_login(user)
    response = client.get("/extractions/help/", HTTP_HX_REQUEST="true")
    assert response.status_code == 200


@pytest.mark.django_db
def test_extraction_help_view_unauthenticated(client: Client):
    response = client.get("/extractions/help/")
    assert response.status_code == 400


@pytest.mark.django_db
def test_unauthenticated_access_redirects_to_login(client: Client):
    user = UserFactory.create(is_active=True)
    job = create_test_extraction_job(owner=user)
    task = create_test_extraction_task(job=job)
    endpoints = [
        "/extractions/jobs/",
        "/extractions/jobs/new/",
        f"/extractions/jobs/{job.pk}/",
        f"/extractions/jobs/{job.pk}/delete/",
        f"/extractions/jobs/{job.pk}/verify/",
        f"/extractions/jobs/{job.pk}/results/",
        f"/extractions/tasks/{task.pk}/",
        "/extractions/update-preferences/",
    ]
    for endpoint in endpoints:
        response = client.get(endpoint)
        assert response.status_code == 302
        assert "/accounts/login/" in response["Location"]


@pytest.mark.django_db
def test_extraction_job_with_tasks(client: Client):
    user = UserFactory.create(is_active=True)
    job = create_test_extraction_job(owner=user)
    create_test_extraction_task(job=job)
    create_test_extraction_task(job=job)
    client.force_login(user)
    response = client.get(f"/extractions/jobs/{job.pk}/")
    assert response.status_code == 200


@pytest.mark.django_db
def test_extraction_instance_detail_view(client: Client):
    user = UserFactory.create(is_active=True)
    client.force_login(user)

    job = create_test_extraction_job(owner=user)
    task = create_test_extraction_task(job=job)
    language = LanguageFactory.create(code="en")
    report = ReportFactory.create(language=language)
    instance = ExtractionInstanceFactory.create(task=task, report=report)

    response = client.get(f"/extractions/instances/{instance.pk}/")
    assert response.status_code == 200


@pytest.mark.django_db
def test_extraction_result_download_csv(client: Client):
    user = UserFactory.create(is_active=True)
    job = create_test_extraction_job(owner=user)
    job.status = ExtractionJob.Status.SUCCESS
    job.save()

    field_one = OutputFieldFactory.create(job=job, name="field_one")
    field_two = OutputFieldFactory.create(job=job, name="field_two")

    task = create_test_extraction_task(job=job)
    language = LanguageFactory.create(code="en")
    report_one = ReportFactory.create(language=language)
    report_two = ReportFactory.create(language=language)
    instance_one = ExtractionInstanceFactory.create(
        task=task,
        report=report_one,
        output={
            field_one.name: "value-1",
            field_two.name: 123,
        },
    )
    instance_two = ExtractionInstanceFactory.create(
        task=task,
        report=report_two,
        output={
            field_one.name: None,
            field_two.name: "value-2",
        },
    )

    client.force_login(user)
    response = client.get(f"/extractions/jobs/{job.pk}/results/download/")

    assert response.status_code == 200
    assert response["Content-Type"] == "text/csv"
    assert response.streaming
    assert (
        response["Content-Disposition"]
        == f'attachment; filename="extraction-job-{job.pk}.csv"'
    )

    content = b"".join(response.streaming_content).decode("utf-8")
    reader = csv.reader(io.StringIO(content))
    rows = list(reader)

    assert rows[0] == ["id", field_one.name, field_two.name]
    assert rows[1] == [str(instance_one.id), "value-1", "123"]
    assert rows[2] == [str(instance_two.id), "", "value-2"]


@pytest.mark.django_db
def test_extraction_result_download_large_dataset(client: Client):
    user = UserFactory.create(is_active=True)
    job = create_test_extraction_job(owner=user)
    job.status = ExtractionJob.Status.SUCCESS
    job.save()

    field = OutputFieldFactory.create(job=job, name="field_one")
    task = create_test_extraction_task(job=job)
    language = LanguageFactory.create(code="en")

    instances = []
    for idx in range(150):
        report = ReportFactory.create(language=language)
        instances.append(
            ExtractionInstanceFactory.create(
                task=task, report=report, output={field.name: f"value-{idx}"}
            )
        )

    client.force_login(user)
    response = client.get(f"/extractions/jobs/{job.pk}/results/download/")

    assert response.status_code == 200
    assert response.streaming

    content = b"".join(response.streaming_content).decode("utf-8")
    rows = list(csv.reader(io.StringIO(content)))

    # header + 150 rows
    assert len(rows) == 151
    assert rows[0] == ["id", field.name]
    # spot check last row corresponds to last instance
    assert rows[-1] == [str(instances[-1].id), f"value-{len(instances) - 1}"]


@pytest.mark.django_db
def test_extraction_result_download_requires_finished_job(client: Client):
    user = UserFactory.create(is_active=True)
    job = create_test_extraction_job(owner=user)
    job.status = ExtractionJob.Status.PENDING
    job.save()

    client.force_login(user)
    response = client.get(f"/extractions/jobs/{job.pk}/results/download/")

    assert response.status_code == 409


@pytest.mark.django_db
def test_extraction_result_download_unauthorized(client: Client):
    owner = UserFactory.create(is_active=True)
    other_user = UserFactory.create(is_active=True)
    job = create_test_extraction_job(owner=owner)
    job.status = ExtractionJob.Status.SUCCESS
    job.save()

    client.force_login(other_user)
    response = client.get(f"/extractions/jobs/{job.pk}/results/download/")

    assert response.status_code == 404


@pytest.mark.django_db
def test_extraction_result_list_view_shows_download_when_finished(client: Client):
    user = UserFactory.create(is_active=True)
    job = create_test_extraction_job(owner=user)
    job.status = ExtractionJob.Status.SUCCESS
    job.save()

    client.force_login(user)
    response = client.get(f"/extractions/jobs/{job.pk}/results/")

    assert response.status_code == 200
    assert "Download CSV" in response.content.decode()
