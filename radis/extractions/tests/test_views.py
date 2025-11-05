from collections.abc import Callable, Iterable
from typing import cast

import pytest
from adit_radis_shared.accounts.factories import GroupFactory, UserFactory
from django.contrib.auth.models import Permission
from django.db import transaction
from django.core.files.base import ContentFile
from django.test import Client
from django.http import FileResponse

from radis.core.models import AnalysisTask
from radis.extractions.factories import (
    ExtractionInstanceFactory,
    ExtractionJobFactory,
    ExtractionTaskFactory,
    OutputFieldFactory,
)
from radis.extractions.models import ExtractionJob, ExtractionResultExport
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
def test_extraction_result_download_small_inline(client: Client):
    user = UserFactory.create(is_active=True)
    job = create_test_extraction_job(owner=user)
    job.status = ExtractionJob.Status.SUCCESS
    job.save()
    field = OutputFieldFactory.create(job=job, name="field_one")
    task = create_test_extraction_task(job=job)
    language = LanguageFactory.create(code="en")
    report = ReportFactory.create(language=language)
    ExtractionInstanceFactory.create(
        task=task,
        report=report,
        output={field.name: "value"},
    )

    client.force_login(user)
    response = client.get(f"/extractions/jobs/{job.pk}/results/download/")

    assert response.status_code == 200
    assert response["Content-Type"] == "text/csv"
    assert b"value" in response.content
    assert job.result_exports.count() == 0


@pytest.mark.django_db
def test_extraction_result_download_queues_export(client: Client, monkeypatch):
    monkeypatch.setattr(
        "radis.extractions.views.SMALL_EXPORT_ROW_LIMIT", 0, raising=False
    )
    callbacks: list[Callable[[], None]] = []

    def immediate_on_commit(func: Callable[[], None]) -> None:
        callbacks.append(func)
        func()

    monkeypatch.setattr(
        "radis.extractions.views.transaction.on_commit", immediate_on_commit, raising=False
    )
    user = UserFactory.create(is_active=True)
    job = create_test_extraction_job(owner=user)
    job.status = ExtractionJob.Status.SUCCESS
    job.save()
    OutputFieldFactory.create(job=job, name="field_one")
    task = create_test_extraction_task(job=job)
    language = LanguageFactory.create(code="en")
    report = ReportFactory.create(language=language)
    ExtractionInstanceFactory.create(task=task, report=report)

    scheduled_exports: list[int] = []

    def fake_delay(self: ExtractionResultExport) -> None:
        scheduled_exports.append(self.pk)

    monkeypatch.setattr(ExtractionResultExport, "delay", fake_delay, raising=False)

    client.force_login(user)
    response = client.get(f"/extractions/jobs/{job.pk}/results/download/")

    assert response.status_code == 202
    assert callbacks
    export = job.result_exports.get()
    assert export.status == ExtractionResultExport.Status.PENDING
    assert scheduled_exports == [export.pk]


@pytest.mark.django_db
def test_extraction_result_download_returns_file_when_ready(client: Client):
    user = UserFactory.create(is_active=True)
    job = create_test_extraction_job(owner=user)
    job.status = ExtractionJob.Status.SUCCESS
    job.save()
    OutputFieldFactory.create(job=job, name="field_one")
    create_test_extraction_task(job=job)

    export = ExtractionResultExport.objects.create(
        job=job,
        requested_by=user,
        status=ExtractionResultExport.Status.PENDING,
    )
    export.file.save(
        f"extraction-job-{job.pk}.csv",
        ContentFile("id,field_one\n1,value-1\n"),
        save=False,
    )
    export.status = ExtractionResultExport.Status.COMPLETED
    export.save()

    client.force_login(user)
    response = client.get(f"/extractions/jobs/{job.pk}/results/download/")

    assert response.status_code == 200
    assert isinstance(response, FileResponse)
    file_response = cast(FileResponse, response)
    assert file_response["Content-Disposition"] == f'attachment; filename="extraction-job-{job.pk}.csv"'
    streaming_content = cast(Iterable[bytes], file_response.streaming_content)
    content = b"".join(streaming_content).decode()
    assert content.startswith("id,field_one")


@pytest.mark.django_db
def test_extraction_result_download_returns_file_when_ready_large(
    client: Client, monkeypatch
):
    monkeypatch.setattr(
        "radis.extractions.views.SMALL_EXPORT_ROW_LIMIT", 0, raising=False
    )
    user = UserFactory.create(is_active=True)
    job = create_test_extraction_job(owner=user)
    job.status = ExtractionJob.Status.SUCCESS
    job.save()
    OutputFieldFactory.create(job=job, name="field_one")
    create_test_extraction_task(job=job)

    export = ExtractionResultExport.objects.create(
        job=job,
        requested_by=user,
        status=ExtractionResultExport.Status.PENDING,
    )
    export.file.save(
        f"extraction-job-{job.pk}.csv",
        ContentFile("id,field_one\n1,value-1\n"),
        save=False,
    )
    export.status = ExtractionResultExport.Status.COMPLETED
    export.save()

    client.force_login(user)
    response = client.get(f"/extractions/jobs/{job.pk}/results/download/")

    assert response.status_code == 200
    assert isinstance(response, FileResponse)
    streaming_content = cast(Iterable[bytes], response.streaming_content)
    content = b"".join(streaming_content).decode()
    assert content.startswith("id,field_one")


@pytest.mark.django_db
def test_extraction_result_download_refresh_triggers_new_export(client: Client, monkeypatch):
    monkeypatch.setattr(
        "radis.extractions.views.SMALL_EXPORT_ROW_LIMIT", 0, raising=False
    )
    callbacks: list[Callable[[], None]] = []

    def immediate_on_commit(func: Callable[[], None]) -> None:
        callbacks.append(func)
        func()

    monkeypatch.setattr(
        "radis.extractions.views.transaction.on_commit", immediate_on_commit, raising=False
    )
    user = UserFactory.create(is_active=True)
    job = create_test_extraction_job(owner=user)
    job.status = ExtractionJob.Status.SUCCESS
    job.save()
    OutputFieldFactory.create(job=job, name="field_one")
    task = create_test_extraction_task(job=job)
    language = LanguageFactory.create(code="en")
    report = ReportFactory.create(language=language)
    ExtractionInstanceFactory.create(task=task, report=report)

    existing_export = ExtractionResultExport.objects.create(
        job=job,
        requested_by=user,
        status=ExtractionResultExport.Status.PENDING,
    )
    existing_export.file.save(
        f"extraction-job-{job.pk}.csv",
        ContentFile("id,field_one\n1,value-1\n"),
        save=False,
    )
    existing_export.status = ExtractionResultExport.Status.COMPLETED
    existing_export.save()

    scheduled_exports: list[int] = []

    def fake_delay(self: ExtractionResultExport) -> None:
        scheduled_exports.append(self.pk)

    monkeypatch.setattr(ExtractionResultExport, "delay", fake_delay, raising=False)

    client.force_login(user)
    response = client.get(f"/extractions/jobs/{job.pk}/results/download/?refresh=1")

    assert response.status_code == 202
    assert callbacks
    assert job.result_exports.count() == 2
    new_export = job.result_exports.first()
    assert new_export is not None
    assert new_export.status == ExtractionResultExport.Status.PENDING
    assert scheduled_exports == [new_export.pk]


@pytest.mark.django_db
def test_extraction_result_download_in_progress(client: Client, monkeypatch):
    monkeypatch.setattr(
        "radis.extractions.views.SMALL_EXPORT_ROW_LIMIT", 0, raising=False
    )
    callbacks: list[Callable[[], None]] = []

    def immediate_on_commit(func: Callable[[], None]) -> None:
        callbacks.append(func)
        func()

    monkeypatch.setattr(
        "radis.extractions.views.transaction.on_commit", immediate_on_commit, raising=False
    )
    user = UserFactory.create(is_active=True)
    job = create_test_extraction_job(owner=user)
    job.status = ExtractionJob.Status.SUCCESS
    job.save()
    OutputFieldFactory.create(job=job, name="field_one")
    task = create_test_extraction_task(job=job)
    language = LanguageFactory.create(code="en")
    report = ReportFactory.create(language=language)
    ExtractionInstanceFactory.create(task=task, report=report)

    ExtractionResultExport.objects.create(
        job=job,
        requested_by=user,
        status=ExtractionResultExport.Status.PROCESSING,
    )

    client.force_login(user)
    response = client.get(f"/extractions/jobs/{job.pk}/results/download/")

    assert response.status_code == 202
    assert b"in progress" in response.content


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
    content = response.content.decode()
    assert "Download CSV" in content


@pytest.mark.django_db
def test_extraction_result_list_view_shows_ready_download(
    client: Client, monkeypatch
):
    monkeypatch.setattr(
        "radis.extractions.views.SMALL_EXPORT_ROW_LIMIT", 0, raising=False
    )
    user = UserFactory.create(is_active=True)
    job = create_test_extraction_job(owner=user)
    job.status = ExtractionJob.Status.SUCCESS
    job.save()
    task = create_test_extraction_task(job=job)
    language = LanguageFactory.create(code="en")
    report = ReportFactory.create(language=language)
    ExtractionInstanceFactory.create(task=task, report=report)

    export = ExtractionResultExport.objects.create(
        job=job,
        requested_by=user,
        status=ExtractionResultExport.Status.COMPLETED,
    )
    export.file.save(
        f"extraction-job-{job.pk}.csv",
        ContentFile("id,field\n1,value\n"),
        save=False,
    )
    export.save()

    client.force_login(user)
    response = client.get(f"/extractions/jobs/{job.pk}/results/")

    assert response.status_code == 200
    content = response.content.decode()
    assert "Download CSV" in content
    assert "Refresh Export" in content


@pytest.mark.django_db
def test_extraction_result_list_view_shows_failure_alert(
    client: Client, monkeypatch
):
    monkeypatch.setattr(
        "radis.extractions.views.SMALL_EXPORT_ROW_LIMIT", 0, raising=False
    )
    user = UserFactory.create(is_active=True)
    job = create_test_extraction_job(owner=user)
    job.status = ExtractionJob.Status.SUCCESS
    job.save()
    task = create_test_extraction_task(job=job)
    language = LanguageFactory.create(code="en")
    report = ReportFactory.create(language=language)
    ExtractionInstanceFactory.create(task=task, report=report)

    ExtractionResultExport.objects.create(
        job=job,
        requested_by=user,
        status=ExtractionResultExport.Status.FAILED,
        error_message="Boom",
    )

    client.force_login(user)
    response = client.get(f"/extractions/jobs/{job.pk}/results/")

    assert response.status_code == 200
    content = response.content.decode()
    assert "failed" in content.lower()
