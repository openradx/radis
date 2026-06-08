"""End-to-end tests for the report HTTP API.

These tests intentionally exercise behavior through Django's `Client`,
so they pass against both the legacy DRF viewset and the ADRF rewrite.
They lock the wire contract before the swap and prove it survives after.

The `_is_async` shape guards at the bottom fail until
`radis.reports.api.views` exists with `async def` handlers — they drive
the rewrite TDD-style.
"""
import importlib
import inspect
import json
from typing import Any

import pytest
from adit_radis_shared.accounts.factories import GroupFactory, UserFactory
from adit_radis_shared.accounts.models import User
from adit_radis_shared.token_authentication.models import Token
from django.contrib.auth.models import Group
from django.test import Client
from django.urls import reverse

from radis.reports.models import Report
from radis.reports.site import (
    DocumentFetcher,
    ReportsCreatedHandler,
    ReportsDeletedHandler,
    document_fetchers,
    reports_created_handlers,
    reports_deleted_handlers,
)


def _make_payload(document_id: str = "DOC-1", body: str = "Report body") -> dict[str, Any]:
    return {
        "document_id": document_id,
        "language": "en",
        "groups": [],  # populated by tests after group is known
        "pacs_aet": "PACS",
        "pacs_name": "Test PACS",
        "pacs_link": "",
        "patient_id": "P1",
        "patient_birth_date": "1980-01-01",
        "patient_sex": "M",
        "study_description": "Study 1",
        "study_datetime": "2024-01-01T00:00:00Z",
        "study_instance_uid": "1.2.3.4",
        "accession_number": "ACC1",
        "modalities": ["CT"],
        "metadata": {"ris_filename": "file1"},
        "body": body,
    }


def _staff_user_and_token() -> tuple[User, Group, str]:
    user = UserFactory.create(is_active=True, is_staff=True)
    group = GroupFactory.create()
    user.groups.add(group)
    _, token = Token.objects.create_token(user, "report api test", None)
    return user, group, token


def _non_staff_user_and_token() -> tuple[User, str]:
    user = UserFactory.create(is_active=True, is_staff=False)
    _, token = Token.objects.create_token(user, "non staff report api test", None)
    return user, token


# ---------------------------------------------------------------------------
# URL resolution
# ---------------------------------------------------------------------------

def test_report_list_url_resolves():
    assert reverse("report-list") == "/api/reports/"


def test_report_bulk_upsert_url_resolves():
    assert reverse("report-bulk-upsert") == "/api/reports/bulk-upsert/"


def test_report_detail_url_resolves():
    assert reverse("report-detail", args=["DOC-1"]) == "/api/reports/DOC-1/"


# ---------------------------------------------------------------------------
# POST /api/reports/  (create)
# ---------------------------------------------------------------------------

@pytest.mark.django_db
def test_post_creates_report_and_fires_created_handler(
    client: Client, django_capture_on_commit_callbacks
):
    _, group, token = _staff_user_and_token()
    captured: list[Report] = []
    handler = ReportsCreatedHandler(
        name="test-created", handle=lambda reports: captured.extend(reports)
    )
    reports_created_handlers.append(handler)
    try:
        payload = _make_payload(document_id="DOC-CREATE")
        payload["groups"] = [group.pk]

        with django_capture_on_commit_callbacks(execute=True):
            response = client.post(
                "/api/reports/",
                data=json.dumps(payload),
                content_type="application/json",
                headers={"Authorization": f"Token {token}"},
            )

        assert response.status_code == 201
        body = response.json()
        assert body["document_id"] == "DOC-CREATE"
        assert body["language"] == "en"
        assert body["modalities"] == ["CT"]
        assert body["metadata"] == {"ris_filename": "file1"}
        assert Report.objects.filter(document_id="DOC-CREATE").exists()
        assert [r.document_id for r in captured] == ["DOC-CREATE"]
    finally:
        reports_created_handlers.remove(handler)


# ---------------------------------------------------------------------------
# GET /api/reports/{document_id}/
# ---------------------------------------------------------------------------

@pytest.mark.django_db
def test_get_returns_existing_report(client: Client):
    _, group, token = _staff_user_and_token()
    payload = _make_payload(document_id="DOC-GET")
    payload["groups"] = [group.pk]
    client.post(
        "/api/reports/",
        data=json.dumps(payload),
        content_type="application/json",
        headers={"Authorization": f"Token {token}"},
    )

    response = client.get(
        "/api/reports/DOC-GET/",
        headers={"Authorization": f"Token {token}"},
    )

    assert response.status_code == 200
    assert response.json()["document_id"] == "DOC-GET"


@pytest.mark.django_db
def test_get_missing_report_returns_404(client: Client):
    _, _, token = _staff_user_and_token()
    response = client.get(
        "/api/reports/DOES-NOT-EXIST/",
        headers={"Authorization": f"Token {token}"},
    )
    assert response.status_code == 404


@pytest.mark.django_db
def test_get_full_includes_documents_from_fetchers(client: Client):
    _, group, token = _staff_user_and_token()
    payload = _make_payload(document_id="DOC-FULL")
    payload["groups"] = [group.pk]
    client.post(
        "/api/reports/",
        data=json.dumps(payload),
        content_type="application/json",
        headers={"Authorization": f"Token {token}"},
    )

    fetcher = DocumentFetcher(
        source="stub-fetcher",
        fetch=lambda report: {"source_id": report.document_id, "extra": "ok"},
    )
    document_fetchers["stub-fetcher"] = fetcher
    try:
        response = client.get(
            "/api/reports/DOC-FULL/?full=true",
            headers={"Authorization": f"Token {token}"},
        )
    finally:
        document_fetchers.pop("stub-fetcher", None)

    assert response.status_code == 200
    body = response.json()
    assert body["documents"]["stub-fetcher"] == {
        "source_id": "DOC-FULL",
        "extra": "ok",
    }


# ---------------------------------------------------------------------------
# PUT /api/reports/{document_id}/
# ---------------------------------------------------------------------------

@pytest.mark.django_db
def test_put_updates_existing_report(client: Client):
    _, group, token = _staff_user_and_token()
    payload = _make_payload(document_id="DOC-PUT")
    payload["groups"] = [group.pk]
    client.post(
        "/api/reports/",
        data=json.dumps(payload),
        content_type="application/json",
        headers={"Authorization": f"Token {token}"},
    )

    payload["body"] = "Updated body"
    response = client.put(
        "/api/reports/DOC-PUT/",
        data=json.dumps(payload),
        content_type="application/json",
        headers={"Authorization": f"Token {token}"},
    )

    assert response.status_code == 200
    assert response.json()["body"] == "Updated body"
    assert Report.objects.get(document_id="DOC-PUT").body == "Updated body"


@pytest.mark.django_db
def test_put_upsert_creates_when_missing(client: Client):
    _, group, token = _staff_user_and_token()
    payload = _make_payload(document_id="DOC-UPSERT-NEW")
    payload["groups"] = [group.pk]

    response = client.put(
        "/api/reports/DOC-UPSERT-NEW/?upsert=true",
        data=json.dumps(payload),
        content_type="application/json",
        headers={"Authorization": f"Token {token}"},
    )

    assert response.status_code == 201
    assert Report.objects.filter(document_id="DOC-UPSERT-NEW").exists()


@pytest.mark.django_db
def test_put_upsert_missing_as_non_staff_returns_403(client: Client):
    """When a PUT?upsert=true hits an unknown id, DRF re-checks permissions
    as if it were a POST. IsAdminUser must reject the non-staff caller."""
    _, token = _non_staff_user_and_token()
    payload = _make_payload(document_id="DOC-FORBIDDEN")

    response = client.put(
        "/api/reports/DOC-FORBIDDEN/?upsert=true",
        data=json.dumps(payload),
        content_type="application/json",
        headers={"Authorization": f"Token {token}"},
    )

    assert response.status_code == 403
    assert not Report.objects.filter(document_id="DOC-FORBIDDEN").exists()


@pytest.mark.django_db
def test_patch_returns_405(client: Client):
    _, _, token = _staff_user_and_token()
    response = client.patch(
        "/api/reports/DOC-NA/",
        data=json.dumps({"body": "irrelevant"}),
        content_type="application/json",
        headers={"Authorization": f"Token {token}"},
    )
    assert response.status_code == 405


# ---------------------------------------------------------------------------
# DELETE /api/reports/{document_id}/
# ---------------------------------------------------------------------------

@pytest.mark.django_db
def test_delete_removes_report_and_fires_deleted_handler(
    client: Client, django_capture_on_commit_callbacks
):
    _, group, token = _staff_user_and_token()
    payload = _make_payload(document_id="DOC-DEL")
    payload["groups"] = [group.pk]
    client.post(
        "/api/reports/",
        data=json.dumps(payload),
        content_type="application/json",
        headers={"Authorization": f"Token {token}"},
    )

    captured: list[Report] = []
    handler = ReportsDeletedHandler(
        name="test-deleted", handle=lambda reports: captured.extend(reports)
    )
    reports_deleted_handlers.append(handler)
    try:
        with django_capture_on_commit_callbacks(execute=True):
            response = client.delete(
                "/api/reports/DOC-DEL/",
                headers={"Authorization": f"Token {token}"},
            )
    finally:
        reports_deleted_handlers.remove(handler)

    assert response.status_code == 204
    assert not Report.objects.filter(document_id="DOC-DEL").exists()
    assert [r.document_id for r in captured] == ["DOC-DEL"]


# ---------------------------------------------------------------------------
# POST /api/reports/bulk-upsert/
# ---------------------------------------------------------------------------

@pytest.mark.django_db
def test_bulk_upsert_rejects_replace_false(client: Client):
    _, _, token = _staff_user_and_token()
    response = client.post(
        "/api/reports/bulk-upsert/?replace=false",
        data=json.dumps([]),
        content_type="application/json",
        headers={"Authorization": f"Token {token}"},
    )
    assert response.status_code == 400


@pytest.mark.django_db
def test_bulk_upsert_rejects_non_list_payload(client: Client):
    _, _, token = _staff_user_and_token()
    response = client.post(
        "/api/reports/bulk-upsert/",
        data=json.dumps({"document_id": "DOC-NOT-A-LIST"}),
        content_type="application/json",
        headers={"Authorization": f"Token {token}"},
    )
    assert response.status_code == 400


# ---------------------------------------------------------------------------
# Async-shape guards — fail until radis.reports.api.views exists with
# async handlers; prevent silent regressions to sync in the future.
# ---------------------------------------------------------------------------

def test_report_list_post_is_coroutine():
    views = importlib.import_module("radis.reports.api.views")
    assert inspect.iscoroutinefunction(views.ReportListAPIView.post)


def test_report_detail_methods_are_coroutines():
    views = importlib.import_module("radis.reports.api.views")
    assert inspect.iscoroutinefunction(views.ReportDetailAPIView.get)
    assert inspect.iscoroutinefunction(views.ReportDetailAPIView.put)
    assert inspect.iscoroutinefunction(views.ReportDetailAPIView.delete)


def test_report_bulk_upsert_post_is_coroutine():
    views = importlib.import_module("radis.reports.api.views")
    assert inspect.iscoroutinefunction(views.ReportBulkUpsertAPIView.post)
