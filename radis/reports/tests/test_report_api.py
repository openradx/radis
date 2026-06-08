"""End-to-end tests for the report HTTP API.

These tests exercise behavior through Django's `AsyncClient` (HTTP-based
tests) and direct module imports (URL resolution + async-shape guards).
They lock the wire contract for the ADRF rewrite.

The `_is_coroutine` shape guards at the bottom assert each handler is
`async def`, preventing silent regressions to sync.

Why `AsyncClient` and not `Client`: the sync `Client` dispatches an async
view via `async_to_sync`, which nested with our own `database_sync_to_async`
deadlocks asgiref's thread executor under pytest-django. `AsyncClient`
runs the async view in the test's event loop with no outer wrapping.

Why `transaction=True`: the test client's outer `async_to_sync` thread
(for sync Client) and the `database_sync_to_async` thread (for our view)
do not share the test's atomic transaction. With `TransactionTestCase`
semantics there is no hidden wrapping transaction, so any thread sees
real committed state.
"""
import importlib
import inspect
import json
from typing import Any

import pytest
from adit_radis_shared.accounts.factories import GroupFactory, UserFactory
from adit_radis_shared.accounts.models import User
from adit_radis_shared.token_authentication.models import Token
from asgiref.sync import sync_to_async
from django.contrib.auth.models import Group
from django.test import AsyncClient
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

@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_post_creates_report_and_fires_created_handler(
    async_client: AsyncClient, django_capture_on_commit_callbacks
):
    _, group, token = await sync_to_async(_staff_user_and_token)()
    captured: list[Report] = []
    handler = ReportsCreatedHandler(
        name="test-created", handle=lambda reports: captured.extend(reports)
    )
    reports_created_handlers.append(handler)
    try:
        payload = _make_payload(document_id="DOC-CREATE")
        payload["groups"] = [group.pk]

        with django_capture_on_commit_callbacks(execute=True):
            response = await async_client.post(
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
        assert await Report.objects.filter(document_id="DOC-CREATE").aexists()
        assert [r.document_id for r in captured] == ["DOC-CREATE"]
    finally:
        reports_created_handlers.remove(handler)


# ---------------------------------------------------------------------------
# GET /api/reports/{document_id}/
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_get_returns_existing_report(async_client: AsyncClient):
    _, group, token = await sync_to_async(_staff_user_and_token)()
    payload = _make_payload(document_id="DOC-GET")
    payload["groups"] = [group.pk]
    await async_client.post(
        "/api/reports/",
        data=json.dumps(payload),
        content_type="application/json",
        headers={"Authorization": f"Token {token}"},
    )

    response = await async_client.get(
        "/api/reports/DOC-GET/",
        headers={"Authorization": f"Token {token}"},
    )

    assert response.status_code == 200
    assert response.json()["document_id"] == "DOC-GET"


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_get_missing_report_returns_404(async_client: AsyncClient):
    _, _, token = await sync_to_async(_staff_user_and_token)()
    response = await async_client.get(
        "/api/reports/DOES-NOT-EXIST/",
        headers={"Authorization": f"Token {token}"},
    )
    assert response.status_code == 404


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_get_full_includes_documents_from_fetchers(async_client: AsyncClient):
    _, group, token = await sync_to_async(_staff_user_and_token)()
    payload = _make_payload(document_id="DOC-FULL")
    payload["groups"] = [group.pk]
    await async_client.post(
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
        response = await async_client.get(
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

@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_put_updates_existing_report(async_client: AsyncClient):
    _, group, token = await sync_to_async(_staff_user_and_token)()
    payload = _make_payload(document_id="DOC-PUT")
    payload["groups"] = [group.pk]
    await async_client.post(
        "/api/reports/",
        data=json.dumps(payload),
        content_type="application/json",
        headers={"Authorization": f"Token {token}"},
    )

    payload["body"] = "Updated body"
    response = await async_client.put(
        "/api/reports/DOC-PUT/",
        data=json.dumps(payload),
        content_type="application/json",
        headers={"Authorization": f"Token {token}"},
    )

    assert response.status_code == 200
    assert response.json()["body"] == "Updated body"
    updated = await Report.objects.aget(document_id="DOC-PUT")
    assert updated.body == "Updated body"


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_put_upsert_creates_when_missing(async_client: AsyncClient):
    _, group, token = await sync_to_async(_staff_user_and_token)()
    payload = _make_payload(document_id="DOC-UPSERT-NEW")
    payload["groups"] = [group.pk]

    response = await async_client.put(
        "/api/reports/DOC-UPSERT-NEW/?upsert=true",
        data=json.dumps(payload),
        content_type="application/json",
        headers={"Authorization": f"Token {token}"},
    )

    assert response.status_code == 201
    assert await Report.objects.filter(document_id="DOC-UPSERT-NEW").aexists()


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_put_upsert_missing_as_non_staff_returns_403(async_client: AsyncClient):
    """When a PUT?upsert=true hits an unknown id, DRF re-checks permissions
    as if it were a POST. IsAdminUser must reject the non-staff caller."""
    _, token = await sync_to_async(_non_staff_user_and_token)()
    payload = _make_payload(document_id="DOC-FORBIDDEN")

    response = await async_client.put(
        "/api/reports/DOC-FORBIDDEN/?upsert=true",
        data=json.dumps(payload),
        content_type="application/json",
        headers={"Authorization": f"Token {token}"},
    )

    assert response.status_code == 403
    assert not await Report.objects.filter(document_id="DOC-FORBIDDEN").aexists()


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_patch_returns_405(async_client: AsyncClient):
    _, _, token = await sync_to_async(_staff_user_and_token)()
    response = await async_client.patch(
        "/api/reports/DOC-NA/",
        data=json.dumps({"body": "irrelevant"}),
        content_type="application/json",
        headers={"Authorization": f"Token {token}"},
    )
    assert response.status_code == 405


# ---------------------------------------------------------------------------
# DELETE /api/reports/{document_id}/
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_delete_removes_report_and_fires_deleted_handler(
    async_client: AsyncClient, django_capture_on_commit_callbacks
):
    _, group, token = await sync_to_async(_staff_user_and_token)()
    payload = _make_payload(document_id="DOC-DEL")
    payload["groups"] = [group.pk]
    await async_client.post(
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
            response = await async_client.delete(
                "/api/reports/DOC-DEL/",
                headers={"Authorization": f"Token {token}"},
            )
    finally:
        reports_deleted_handlers.remove(handler)

    assert response.status_code == 204
    assert not await Report.objects.filter(document_id="DOC-DEL").aexists()
    assert [r.document_id for r in captured] == ["DOC-DEL"]


# ---------------------------------------------------------------------------
# POST /api/reports/bulk-upsert/
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_bulk_upsert_rejects_replace_false(async_client: AsyncClient):
    _, _, token = await sync_to_async(_staff_user_and_token)()
    response = await async_client.post(
        "/api/reports/bulk-upsert/?replace=false",
        data=json.dumps([]),
        content_type="application/json",
        headers={"Authorization": f"Token {token}"},
    )
    assert response.status_code == 400


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_bulk_upsert_rejects_non_list_payload(async_client: AsyncClient):
    _, _, token = await sync_to_async(_staff_user_and_token)()
    response = await async_client.post(
        "/api/reports/bulk-upsert/",
        data=json.dumps({"document_id": "DOC-NOT-A-LIST"}),
        content_type="application/json",
        headers={"Authorization": f"Token {token}"},
    )
    assert response.status_code == 400


# ---------------------------------------------------------------------------
# Async-shape guards — prevent silent regressions to sync handlers.
# ---------------------------------------------------------------------------

def test_report_viewset_methods_are_coroutines():
    """Pin every dispatched method on ReportViewSet to async.

    `adrf.mixins.CreateModelMixin` inherits from DRF's sync mixin, so the
    class technically has both `create` (sync) and `acreate` (async) on the
    MRO. ADRF's `view_is_async` flips the dispatcher to the async path only
    if *all* of our overrides are coroutines. If a future contributor
    accidentally overrides the sync sibling (`create`/`retrieve`/`update`/
    `destroy`), the dispatch would silently switch to sync and break the
    inline-embedding follow-up.
    """
    views = importlib.import_module("radis.reports.api.views")
    vs = views.ReportViewSet
    for name in ("acreate", "aretrieve", "aupdate", "adestroy", "bulk_upsert"):
        assert inspect.iscoroutinefunction(getattr(vs, name)), (
            f"ReportViewSet.{name} must be async"
        )
