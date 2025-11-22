import logging
import os
from collections.abc import Callable

import pytest
from adit_radis_shared.accounts.factories import GroupFactory, UserFactory
from django.contrib.auth.models import Permission
from django.core.files.base import ContentFile
from django.test import Client
from django.test.utils import override_settings

from radis.core.models import AnalysisTask
from radis.extractions.factories import (
    ExtractionInstanceFactory,
    ExtractionJobFactory,
    ExtractionTaskFactory,
    OutputFieldFactory,
)
from radis.extractions.models import (
    ExtractionInstance,
    ExtractionJob,
    ExtractionResultExport,
)
from radis.extractions.tasks import process_extraction_result_export
from radis.extractions.utils.csv import sanitize_csv_value
from radis.reports.factories import LanguageFactory, ReportFactory


def _run_on_commit_immediately(monkeypatch, callbacks: list[Callable[[], None]]) -> None:
    """Execute on_commit callbacks right away during tests."""

    def immediate(func: Callable[[], None]) -> None:
        callbacks.append(func)
        func()

    monkeypatch.setattr(
        "radis.extractions.views.transaction.on_commit", immediate, raising=False
    )


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

    if response.status_code == 202:
        # Background export scheduled unexpectedly; fetch the newly generated file
        export = job.result_exports.order_by("-created_at").first()
        assert export is not None
        response = client.get(f"/extractions/jobs/{job.pk}/results/download/")

    assert response.status_code == 200
    assert response["Content-Type"] == "text/csv"
    assert b"value" in response.content
    assert job.result_exports.count() == 0


@pytest.mark.django_db
def test_extraction_result_download_sanitizes_formula_like_values(client: Client):
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
        output={field.name: "=SUM(1,1)"},
    )

    client.force_login(user)
    response = client.get(f"/extractions/jobs/{job.pk}/results/download/")
    if response.status_code == 202:
        response = client.get(f"/extractions/jobs/{job.pk}/results/download/")
    assert response.status_code == 200
    content = response.content.decode()
    assert "'=SUM(1,1)" in content
    assert not job.result_exports.exists()


@pytest.mark.django_db
def test_extraction_result_download_handles_large_values(client: Client):
    user = UserFactory.create(is_active=True)
    job = create_test_extraction_job(owner=user)
    job.status = ExtractionJob.Status.SUCCESS
    job.save()
    field = OutputFieldFactory.create(job=job, name="field_one")
    task = create_test_extraction_task(job=job)
    language = LanguageFactory.create(code="en")
    report = ReportFactory.create(language=language)
    large_value = "A" * 200_000
    ExtractionInstanceFactory.create(
        task=task,
        report=report,
        output={field.name: large_value},
    )

    client.force_login(user)
    response = client.get(f"/extractions/jobs/{job.pk}/results/download/")
    if response.status_code == 202:
        response = client.get(f"/extractions/jobs/{job.pk}/results/download/")

    assert response.status_code == 200
    assert large_value in response.content.decode()
    assert not job.result_exports.exists()


@pytest.mark.django_db
def test_extraction_result_download_empty_inline(client: Client):
    user = UserFactory.create(is_active=True)
    job = create_test_extraction_job(owner=user)
    job.status = ExtractionJob.Status.SUCCESS
    job.save()
    field = OutputFieldFactory.create(job=job, name="field_one")
    create_test_extraction_task(job=job)

    client.force_login(user)
    response = client.get(f"/extractions/jobs/{job.pk}/results/download/")
    if response.status_code == 202:
        response = client.get(f"/extractions/jobs/{job.pk}/results/download/")

    assert response.status_code == 200
    content = response.content.decode()
    assert content == f"id,{field.name}\n"
    assert job.result_exports.count() == 0


@pytest.mark.django_db
def test_extraction_result_download_queues_export(client: Client, monkeypatch):
    callbacks: list[Callable[[], None]] = []
    _run_on_commit_immediately(monkeypatch, callbacks)
    with override_settings(EXTRACTION_SMALL_EXPORT_ROW_LIMIT=0):
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
    assert response["Content-Disposition"] == (
        f'attachment; filename="extraction-job-{job.pk}.csv"'
    )


@pytest.mark.django_db
def test_extraction_result_download_handles_missing_completed_file(
    client: Client, monkeypatch, caplog
):
    callbacks: list[Callable[[], None]] = []
    _run_on_commit_immediately(monkeypatch, callbacks)
    with override_settings(EXTRACTION_SMALL_EXPORT_ROW_LIMIT=0):
        user = UserFactory.create(is_active=True)
        job = create_test_extraction_job(owner=user)
        job.status = ExtractionJob.Status.SUCCESS
        job.save()
        OutputFieldFactory.create(job=job, name="field_one")
        task = create_test_extraction_task(job=job)
        language = LanguageFactory.create(code="en")
        report = ReportFactory.create(language=language)
        ExtractionInstanceFactory.create(task=task, report=report)

        original_export = ExtractionResultExport.objects.create(
            job=job,
            requested_by=user,
            status=ExtractionResultExport.Status.COMPLETED,
        )
        original_export.file.save(
            f"extraction-job-{job.pk}.csv",
            ContentFile("id,field_one\n1,value-1\n"),
            save=False,
        )
        original_export.save()
        original_export.file.delete(save=False)

        scheduled_exports: list[int] = []

        def fake_delay(self: ExtractionResultExport) -> None:
            scheduled_exports.append(self.pk)

        monkeypatch.setattr(ExtractionResultExport, "delay", fake_delay, raising=False)

        client.force_login(user)
        with caplog.at_level(logging.WARNING):
            response = client.get(f"/extractions/jobs/{job.pk}/results/download/")

    assert response.status_code == 202
    assert callbacks
    assert scheduled_exports
    new_export = job.result_exports.order_by("-created_at").first()
    assert new_export is not None
    assert new_export.pk == original_export.pk
    assert new_export.status == ExtractionResultExport.Status.PENDING
    message_text = response.content.decode()
    assert message_text.startswith("Export file missing")


@pytest.mark.django_db
def test_extraction_result_download_retries_failed_export(client: Client, monkeypatch):
    callbacks: list[Callable[[], None]] = []
    _run_on_commit_immediately(monkeypatch, callbacks)
    with override_settings(EXTRACTION_SMALL_EXPORT_ROW_LIMIT=0):
        user = UserFactory.create(is_active=True)
        job = create_test_extraction_job(owner=user)
        job.status = ExtractionJob.Status.SUCCESS
        job.save()
        OutputFieldFactory.create(job=job, name="field_one")
        task = create_test_extraction_task(job=job)
        language = LanguageFactory.create(code="en")
        report = ReportFactory.create(language=language)
        ExtractionInstanceFactory.create(task=task, report=report)

        failed_export = ExtractionResultExport.objects.create(
            job=job,
            requested_by=user,
            status=ExtractionResultExport.Status.FAILED,
        )

        scheduled_exports: list[int] = []

        def fake_delay(self: ExtractionResultExport) -> None:
            scheduled_exports.append(self.pk)

        monkeypatch.setattr(ExtractionResultExport, "delay", fake_delay, raising=False)

        client.force_login(user)
        response = client.get(f"/extractions/jobs/{job.pk}/results/download/")

    assert response.status_code == 202
    assert callbacks
    assert scheduled_exports
    new_export_id = scheduled_exports[-1]
    new_export = ExtractionResultExport.objects.get(pk=new_export_id)
    assert new_export.pk != failed_export.pk
    assert new_export.status == ExtractionResultExport.Status.PENDING
    assert "scheduled" in response.content.decode().lower()


@pytest.mark.django_db
def test_extraction_result_download_returns_file_when_ready_large(
    client: Client, monkeypatch
):
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

    with override_settings(EXTRACTION_SMALL_EXPORT_ROW_LIMIT=0):
        client.force_login(user)
        response = client.get(f"/extractions/jobs/{job.pk}/results/download/")

    assert response.status_code == 200
    assert response["Content-Disposition"] == (
        f'attachment; filename="extraction-job-{job.pk}.csv"'
    )


@pytest.mark.django_db
def test_extraction_result_download_refresh_triggers_new_export(client: Client, monkeypatch):
    callbacks: list[Callable[[], None]] = []
    _run_on_commit_immediately(monkeypatch, callbacks)
    with override_settings(EXTRACTION_SMALL_EXPORT_ROW_LIMIT=0):
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
    callbacks: list[Callable[[], None]] = []
    _run_on_commit_immediately(monkeypatch, callbacks)
    with override_settings(EXTRACTION_SMALL_EXPORT_ROW_LIMIT=0):
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
    user = UserFactory.create(is_active=True)
    job = create_test_extraction_job(owner=user)
    job.status = ExtractionJob.Status.SUCCESS
    job.save()
    with override_settings(EXTRACTION_SMALL_EXPORT_ROW_LIMIT=0):
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
    user = UserFactory.create(is_active=True)
    job = create_test_extraction_job(owner=user)
    job.status = ExtractionJob.Status.SUCCESS
    job.save()
    with override_settings(EXTRACTION_SMALL_EXPORT_ROW_LIMIT=0):
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


@override_settings(EXTRACTION_RESULTS_EXPORT_CHUNK_SIZE=2)
@pytest.mark.django_db
def test_process_extraction_result_export_writes_expected_csv():
    user = UserFactory.create(is_active=True)
    job = create_test_extraction_job(owner=user)
    job.status = ExtractionJob.Status.SUCCESS
    job.save()

    fields = [
        OutputFieldFactory.create(job=job, name=f"field_{idx}") for idx in range(5)
    ]
    task = create_test_extraction_task(job=job)
    language = LanguageFactory.create(code="en")

    instances = []
    for idx in range(5):
        report = ReportFactory.create(language=language)
        output = {field.name: f"value-{idx}-{field.name}" for field in fields}
        instance = ExtractionInstanceFactory.create(
            task=task,
            report=report,
            output=output,
        )
        instances.append(instance)

    export = ExtractionResultExport.objects.create(job=job, requested_by=user)

    process_extraction_result_export(export.pk)

    export.refresh_from_db()
    assert export.status == ExtractionResultExport.Status.COMPLETED
    assert export.row_count == len(instances)
    assert export.file

    with export.file.open("rb") as exported_file:
        content = exported_file.read().decode("utf-8")

    header = ",".join(["id"] + [field.name for field in fields])
    expected_rows = [header]
    for instance in ExtractionInstance.objects.filter(task__job=job).order_by("id"):
        row = [sanitize_csv_value(instance.pk)]
        for field in fields:
            row.append(sanitize_csv_value(instance.output.get(field.name)))
        expected_rows.append(",".join(row))
    expected_csv = "\n".join(expected_rows) + "\n"

    assert content == expected_csv


@override_settings(EXTRACTION_RESULTS_EXPORT_CHUNK_SIZE=1)
@pytest.mark.django_db
def test_process_extraction_result_export_sanitizes_formula_like_values():
    user = UserFactory.create(is_active=True)
    job = create_test_extraction_job(owner=user)
    job.status = ExtractionJob.Status.SUCCESS
    job.save()

    field = OutputFieldFactory.create(job=job, name="formula_field")
    task = create_test_extraction_task(job=job)
    language = LanguageFactory.create(code="en")
    report = ReportFactory.create(language=language)
    ExtractionInstanceFactory.create(
        task=task,
        report=report,
        output={field.name: "+CMD|' /C calc'!A0"},
    )

    export = ExtractionResultExport.objects.create(job=job, requested_by=user)

    process_extraction_result_export(export.pk)

    export.refresh_from_db()
    assert export.status == ExtractionResultExport.Status.COMPLETED

    with export.file.open("rb") as exported_file:
        content = exported_file.read().decode("utf-8")
    assert "'+CMD|' /C calc'!A0" in content


@override_settings(EXTRACTION_RESULTS_EXPORT_CHUNK_SIZE=2)
@pytest.mark.django_db
def test_process_extraction_result_export_handles_large_value():
    user = UserFactory.create(is_active=True)
    job = create_test_extraction_job(owner=user)
    job.status = ExtractionJob.Status.SUCCESS
    job.save()

    field = OutputFieldFactory.create(job=job, name="large_field")
    task = create_test_extraction_task(job=job)
    language = LanguageFactory.create(code="en")
    large_value = "B" * 250_000
    report = ReportFactory.create(language=language)
    ExtractionInstanceFactory.create(
        task=task,
        report=report,
        output={field.name: large_value},
    )

    export = ExtractionResultExport.objects.create(job=job, requested_by=user)

    process_extraction_result_export(export.pk)

    export.refresh_from_db()
    assert export.status == ExtractionResultExport.Status.COMPLETED
    with export.file.open("rb") as exported_file:
        content = exported_file.read().decode("utf-8")
    assert large_value in content


@pytest.mark.django_db
def test_extraction_result_export_delete_removes_file():
    user = UserFactory.create(is_active=True)
    job = create_test_extraction_job(owner=user)
    job.status = ExtractionJob.Status.SUCCESS
    job.save()

    export = ExtractionResultExport.objects.create(job=job, requested_by=user)
    export.file.save(
        f"extraction-job-{job.pk}.csv", ContentFile("id,field\n"), save=True
    )
    file_name = export.file.name
    storage = export.file.storage
    assert storage.exists(file_name)

    export.delete()

    assert not storage.exists(file_name)


@pytest.mark.django_db
def test_extraction_result_export_save_replaces_old_file():
    user = UserFactory.create(is_active=True)
    job = create_test_extraction_job(owner=user)
    job.status = ExtractionJob.Status.SUCCESS
    job.save()

    export = ExtractionResultExport.objects.create(job=job, requested_by=user)
    export.file.save(
        f"extraction-job-{job.pk}.csv", ContentFile("id,field\n"), save=True
    )
    original_name = export.file.name
    storage = export.file.storage
    assert storage.exists(original_name)

    export.file.save(
        f"extraction-job-{job.pk}-updated.csv", ContentFile("id,field\n1,value\n"), save=True
    )
    export.save()

    assert not storage.exists(original_name)
    assert storage.exists(export.file.name)


@override_settings(EXTRACTION_RESULTS_EXPORT_CHUNK_SIZE=1)
@pytest.mark.django_db
def test_process_extraction_result_export_logs_permission_error_on_cleanup(
    monkeypatch
):
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

    export = ExtractionResultExport.objects.create(job=job, requested_by=user)

    def fake_remove(path):
        raise PermissionError("denied")

    monkeypatch.setattr(os, "remove", fake_remove)
    logger = logging.getLogger("radis.extractions.tasks")
    original_warning = logger.warning
    calls: list[tuple[tuple, dict]] = []

    def fake_warning(*args, **kwargs):
        calls.append((args, kwargs))
        return original_warning(*args, **kwargs)

    monkeypatch.setattr(logger, "warning", fake_warning)

    process_extraction_result_export(export.pk)

    export.refresh_from_db()
    assert export.status == ExtractionResultExport.Status.COMPLETED
    assert any(
        args and args[0] == "Insufficient permissions to remove temporary export file %s"
        for args, _ in calls
    )
