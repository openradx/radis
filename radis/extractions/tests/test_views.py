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


@pytest.mark.django_db
@pytest.mark.parametrize(
    "url",
    ["/extractions/jobs/new/search-preview/", "/extractions/jobs/new/generate-query/"],
)
def test_htmx_endpoints_require_permission(client: Client, url: str):
    """Regression test for the add_extractionjob requirement on the HTMX endpoints."""
    user = UserFactory.create(is_active=True)
    client.force_login(user)

    method = client.get if url.endswith("search-preview/") else client.post
    response = method(url)
    assert response.status_code == 403


@pytest.mark.django_db
@pytest.mark.parametrize(
    "url",
    ["/extractions/jobs/new/search-preview/", "/extractions/jobs/new/generate-query/"],
)
def test_htmx_endpoints_redirect_anonymous_users(client: Client, url: str):
    method = client.get if url.endswith("search-preview/") else client.post
    response = method(url)
    assert response.status_code == 302
    assert "/login" in response["Location"] or "/accounts" in response["Location"]


@override_settings(DEBUG_TOOLBAR_CONFIG={"SHOW_TOOLBAR_CALLBACK": _hide_toolbar})
@pytest.mark.django_db
def test_extraction_result_download_escapes_spreadsheet_formulas(client: Client):
    """Cells starting with =, +, -, @ are neutralized against CSV injection."""
    user = UserFactory.create(is_active=True)
    job = create_test_extraction_job(owner=user)

    OutputFieldFactory.create(job=job, name="finding")
    OutputFieldFactory.create(job=job, name="note")

    task = create_test_extraction_task(job=job)
    language = LanguageFactory.create(code="en")
    report = ReportFactory.create(language=language)
    ExtractionInstanceFactory.create(
        task=task,
        report=report,
        is_processed=True,
        output={
            "finding": '=HYPERLINK("http://evil")',
            # Some importers trim leading whitespace before evaluating a cell,
            # so a space-cloaked formula must be escaped as well.
            "note": ' =HYPERLINK("http://evil2")',
        },
    )

    client.force_login(user)
    response = client.get(f"/extractions/jobs/{job.pk}/results/download/")
    assert response.status_code == 200

    csv_text = _collect_csv(response)
    assert "'=HYPERLINK" in csv_text
    assert ',"=HYPERLINK' not in csv_text
    assert "' =HYPERLINK" in csv_text
    assert '," =HYPERLINK' not in csv_text


@override_settings(DEBUG_TOOLBAR_CONFIG={"SHOW_TOOLBAR_CALLBACK": _hide_toolbar})
@pytest.mark.django_db
def test_generate_query_merges_into_current_session_state(client: Client):
    """Wizard writes that land while the LLM call is in flight must survive.

    The generation endpoint reads the wizard session, awaits a multi-second
    LLM call, and then persists its result. If it wrote back its pre-await
    snapshot wholesale, a search-step submit that happened during the await
    (current step, step data) would be discarded. It must instead merge only
    the generation keys into the backend's current state.
    """
    from importlib import import_module
    from unittest.mock import patch

    from django.conf import settings as django_settings

    user = UserFactory.create(is_active=True)
    user.user_permissions.add(Permission.objects.get(codename="add_extractionjob"))
    client.force_login(user)

    wizard_key = "wizard_extraction_job_wizard_view"
    session = client.session
    session[wizard_key] = {
        "step": "1",
        "step_data": {"0": {"formset": "data"}},
        "extra_data": {
            "output_fields_data": [
                {
                    "name": "finding",
                    "description": "main finding",
                    "output_type": "T",
                    "selection_options": [],
                    "is_array": False,
                }
            ]
        },
    }
    session.save()
    session_key = session.session_key
    engine = import_module(django_settings.SESSION_ENGINE)

    async def fake_generate(self, fields):
        # Simulate the user submitting the search step mid-generation: the
        # backend now holds newer wizard state than the endpoint's snapshot.
        store = engine.SessionStore(session_key)
        data = await store.aget(wizard_key, {})
        data["step"] = "2"
        data["step_data"]["1"] = {"1-query": "user refined query"}
        await store.aset(wizard_key, data)
        await store.asave()
        return "generated query", {
            "field_count": 1,
            "success": True,
            "generation_method": "llm",
            "error": None,
        }

    with patch(
        "radis.extractions.utils.query_generator.AsyncQueryGenerator.generate_from_fields",
        new=fake_generate,
    ):
        response = client.post("/extractions/jobs/new/generate-query/")

    assert response.status_code == 200

    data = engine.SessionStore(session_key).load()[wizard_key]
    # The concurrent wizard write survived ...
    assert data["step"] == "2"
    assert data["step_data"]["1"] == {"1-query": "user refined query"}
    # ... and the generation outcome was merged in.
    assert data["extra_data"]["generated_query"] == "generated query"
    assert data["extra_data"]["query_generation_attempted"] is True


@pytest.mark.parametrize(
    ("attempted", "succeeded", "current_query", "expected"),
    [
        # First render for a set of output fields: always fire.
        (False, False, "", True),
        (False, False, "typed", True),
        # Attempt succeeded: never re-fire (the static result box is shown).
        (True, True, "", False),
        (True, True, "generated query", False),
        # Attempt failed and the field is still empty: retry (can only fill
        # a gap, e.g. after a transient LLM error).
        (True, False, "", True),
        (True, False, "   ", True),
        # Attempt failed but the user typed a query: leave it alone.
        (True, False, "manual query", False),
    ],
)
def test_query_generation_needed_matrix(settings, attempted, succeeded, current_query, expected):
    from radis.extractions.views import ExtractionJobWizardView

    settings.ENABLE_AUTO_QUERY_GENERATION = True
    assert (
        ExtractionJobWizardView._query_generation_needed(
            attempted=attempted, succeeded=succeeded, current_query=current_query
        )
        is expected
    )


def test_query_generation_needed_respects_disabled_setting(settings):
    from radis.extractions.views import ExtractionJobWizardView

    settings.ENABLE_AUTO_QUERY_GENERATION = False
    assert (
        ExtractionJobWizardView._query_generation_needed(
            attempted=False, succeeded=False, current_query=""
        )
        is False
    )
