import pytest
from adit_radis_shared.accounts.factories import GroupFactory, UserFactory
from django.contrib.auth.models import Permission
from django.test import Client, override_settings

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


def _hide_toolbar(_request):
    return False


def _collect_csv(response) -> str:
    chunks: list[bytes] = []
    for chunk in response.streaming_content:
        if isinstance(chunk, bytes):
            chunks.append(chunk)
        else:
            chunks.append(chunk.encode("utf-8"))
    csv_bytes = b"".join(chunks)
    return csv_bytes.decode("utf-8-sig")


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


@override_settings(DEBUG_TOOLBAR_CONFIG={"SHOW_TOOLBAR_CALLBACK": _hide_toolbar})
@pytest.mark.django_db
def test_extraction_result_download_view(client: Client):
    user = UserFactory.create(is_active=True)
    job = create_test_extraction_job(owner=user)

    OutputFieldFactory.create(job=job, name="field_one")
    OutputFieldFactory.create(job=job, name="field_two")
    OutputFieldFactory.create(job=job, name="field_bool")

    task = create_test_extraction_task(job=job)
    language = LanguageFactory.create(code="en")
    report = ReportFactory.create(language=language)
    instance = ExtractionInstanceFactory.create(
        task=task,
        report=report,
        is_processed=True,
        output={"field_one": "value", "field_two": 42, "field_bool": False},
    )

    client.force_login(user)
    response = client.get(f"/extractions/jobs/{job.pk}/results/download/")
    assert response.status_code == 200
    assert response["Content-Type"].startswith("text/csv")
    assert f"extraction_job_{job.pk}" in response["Content-Disposition"]

    csv_text = _collect_csv(response)
    lines = [line.strip() for line in csv_text.strip().splitlines()]
    assert lines[0] == "instance_id,report_id,is_processed,field_one,field_two,field_bool"
    assert lines[1] == f"{instance.pk},{instance.report.pk},yes,value,42,no"


@override_settings(DEBUG_TOOLBAR_CONFIG={"SHOW_TOOLBAR_CALLBACK": _hide_toolbar})
@pytest.mark.django_db
def test_extraction_result_download_view_unauthorized(client: Client):
    owner = UserFactory.create(is_active=True)
    other_user = UserFactory.create(is_active=True)
    job = create_test_extraction_job(owner=owner)
    client.force_login(other_user)
    response = client.get(f"/extractions/jobs/{job.pk}/results/download/")
    assert response.status_code == 404


@override_settings(DEBUG_TOOLBAR_CONFIG={"SHOW_TOOLBAR_CALLBACK": _hide_toolbar})
@pytest.mark.django_db
def test_extraction_result_download_view_no_instances(client: Client):
    user = UserFactory.create(is_active=True)
    job = create_test_extraction_job(owner=user)
    OutputFieldFactory.create(job=job, name="field_one")

    client.force_login(user)
    response = client.get(f"/extractions/jobs/{job.pk}/results/download/")
    assert response.status_code == 200

    csv_text = _collect_csv(response)
    assert csv_text.strip() == "instance_id,report_id,is_processed,field_one"


@override_settings(DEBUG_TOOLBAR_CONFIG={"SHOW_TOOLBAR_CALLBACK": _hide_toolbar})
@pytest.mark.django_db
def test_extraction_result_download_view_no_output_fields(client: Client):
    user = UserFactory.create(is_active=True)
    job = create_test_extraction_job(owner=user)
    task = create_test_extraction_task(job=job)
    language = LanguageFactory.create(code="en")
    report = ReportFactory.create(language=language)
    instance = ExtractionInstanceFactory.create(
        task=task,
        report=report,
        is_processed=False,
        output={},
    )

    client.force_login(user)
    response = client.get(f"/extractions/jobs/{job.pk}/results/download/")
    assert response.status_code == 200

    csv_text = _collect_csv(response)
    lines = csv_text.strip().splitlines()
    assert lines[0] == "instance_id,report_id,is_processed"
    assert lines[1] == f"{instance.pk},{instance.report.pk},no"


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
