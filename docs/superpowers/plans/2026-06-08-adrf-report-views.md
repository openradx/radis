# ADRF Report Views Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the sync DRF `ReportViewSet` with one `adrf.viewsets.GenericViewSet` subclass (plus the create / retrieve / update / destroy async mixins from `adrf.mixins` and a `@action` for `bulk_upsert`) so the report-upload endpoints can `await` the async embedding client from inside the view in a follow-up PR. No client-visible API change in this PR.

**Architecture:** Minimum-diff conversion of the legacy class: same mixin lineup, same `GenericViewSet` base, routing through `adrf.routers.DefaultRouter` (not DRF's — see Task 4 for why). The only structural change is `mixins.* → adrf.mixins.*` and the async-method overrides (`acreate`, `aretrieve`, `aupdate`, `adestroy`, `bulk_upsert`). Use native async ORM (`.aget`) for simple lookups and `channels.db.database_sync_to_async` to wrap DRF serializer + `transaction.atomic()` blocks. The `_bulk_upsert_reports` helper stays in `viewsets.py` (renamed `bulk_upsert_reports` — no separate `bulk.py` file).

**Tech Stack:** Django 5.1+ (CI runs 6.0.1), DRF, ADRF (`adrf.viewsets.GenericViewSet` + `adrf.mixins`), Channels (`database_sync_to_async`), PostgreSQL, Procrastinate, pytest-django.

**Spec:** `docs/superpowers/specs/2026-06-08-adrf-report-views-design.md`

---

## File Structure

| Action | Path | Responsibility |
| --- | --- | --- |
| Modify | `radis/reports/api/viewsets.py` | Single `ReportViewSet` rewritten on top of `adrf.viewsets.GenericViewSet` + the four async mixins from `adrf.mixins` + an `@action` for `bulk_upsert`. The bulk-upsert helper (`bulk_upsert_reports`, renamed from `_bulk_upsert_reports`) stays in the same module — this is a DRF-viewset → ADRF-viewset conversion, not a file restructure. |
| Modify | `radis/reports/api/urls.py` | Keep `DefaultRouter`; register `ReportViewSet` with `basename="report"`. (No real diff vs. legacy.) |
| Modify | `radis/reports/tests/test_bulk_upsert.py` | Update import (`from radis.reports.api.viewsets import _bulk_upsert_reports` → `from radis.reports.api.viewsets import bulk_upsert_reports`). |
| Create | `radis/reports/tests/test_report_api.py` | End-to-end coverage for all five endpoints via Django's `AsyncClient`; plus `inspect.iscoroutinefunction` shape guards on the viewset's async method set. |

Unchanged: `radis/reports/api/serializers.py`, `radis/reports/api/__init__.py`, `radis/urls.py` (mount stays `path("api/reports/", include("radis.reports.api.urls"))`).

The legacy file `radis/reports/api/viewsets.py` is rewritten in place (not renamed to `views.py` or split into `bulk.py` + `views.py`). The file name matches the framework convention (`viewsets.py` for viewset classes) and the diff reads as a sync→async conversion of the same module.

---

## Prerequisites (run once before Task 1)

The test suite runs inside the `web` container via `uv run cli test`, which gates on `helper.check_compose_up()`. Bring the dev stack up first:

```bash
cd /Users/samuelkwong/adit-radis-workspace/projects/radis/.claude/worktrees/feat+adrf-views
uv run cli compose-up -d
```

Confirm a green baseline:

```bash
uv run cli test
```

If the baseline is not green, **stop and report** — do not proceed to Task 1.

---

## Task 1: Rename `_bulk_upsert_reports` → `bulk_upsert_reports` inside `viewsets.py`

A one-line touch-up before the async conversion. The helper currently lives at module scope in `radis/reports/api/viewsets.py` with a leading-underscore name. After the conversion it stays in the same module and is called from `ReportViewSet.bulk_upsert`, so the underscore is misleading — it's the module's de-facto public bulk-upsert entry point.

**Files:**
- Modify: `radis/reports/api/viewsets.py`
- Modify: `radis/reports/tests/test_bulk_upsert.py`

- [ ] **Step 1.1: Rename the function in `radis/reports/api/viewsets.py`**

`def _bulk_upsert_reports(...)` → `def bulk_upsert_reports(...)`. Update the single internal call site (inside the legacy `bulk_upsert` action) the same way.

- [ ] **Step 1.2: Update the test import**

In `radis/reports/tests/test_bulk_upsert.py`, change

```python
from radis.reports.api.viewsets import _bulk_upsert_reports
```

to

```python
from radis.reports.api.viewsets import bulk_upsert_reports
```

and rename the one call site in the test body.

- [ ] **Step 1.3: Lint and commit**

```bash
uv run cli lint
git add radis/reports/api/viewsets.py radis/reports/tests/test_bulk_upsert.py
git commit -m "refactor(reports): drop leading underscore from bulk_upsert_reports"
```

---

## Task 2: Add new test file with regression + async-shape guards

Write the end-to-end coverage that proves the new ADRF views preserve the API contract, plus shape guards that fail until the new view classes exist. The regression tests **already pass** against the current DRF viewset (since the contract is byte-for-byte preserved) — that is the entire point: they lock the contract before the rewrite, then prove it survived after.

**Files:**
- Create: `radis/reports/tests/test_report_api.py`

- [ ] **Step 2.1: Write the test file**

Create `radis/reports/tests/test_report_api.py`:

```python
"""End-to-end tests for the report HTTP API.

These tests intentionally exercise behavior through Django's `Client`,
so they pass against both the legacy DRF viewset and the ADRF rewrite.
They lock the wire contract before the swap and prove it survives after.

The `_is_async` shape guards at the bottom fail until
`radis.reports.api.views` exists with `async def` handlers — they drive
the rewrite TDD-style.
"""
import asyncio
import json
from datetime import date

import pytest
from adit_radis_shared.accounts.factories import GroupFactory, UserFactory
from adit_radis_shared.token_authentication.models import Token
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


def _make_payload(document_id: str = "DOC-1", body: str = "Report body") -> dict:
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


def _staff_user_and_token() -> tuple[object, object, str]:
    user = UserFactory.create(is_active=True, is_staff=True)
    group = GroupFactory.create()
    user.groups.add(group)
    _, token = Token.objects.create_token(user, "report api test", None)
    return user, group, token


def _non_staff_user_and_token() -> tuple[object, str]:
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
def test_post_creates_report_and_fires_created_handler(client: Client):
    _, group, token = _staff_user_and_token()
    captured: list[Report] = []
    handler = ReportsCreatedHandler(
        name="test-created", handle=lambda reports: captured.extend(reports)
    )
    reports_created_handlers.append(handler)
    try:
        payload = _make_payload(document_id="DOC-CREATE")
        payload["groups"] = [group.pk]

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
def test_delete_removes_report_and_fires_deleted_handler(client: Client):
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
    from radis.reports.api.views import ReportListAPIView
    assert asyncio.iscoroutinefunction(ReportListAPIView.post)


def test_report_detail_methods_are_coroutines():
    from radis.reports.api.views import ReportDetailAPIView
    assert asyncio.iscoroutinefunction(ReportDetailAPIView.get)
    assert asyncio.iscoroutinefunction(ReportDetailAPIView.put)
    assert asyncio.iscoroutinefunction(ReportDetailAPIView.delete)


def test_report_bulk_upsert_post_is_coroutine():
    from radis.reports.api.views import ReportBulkUpsertAPIView
    assert asyncio.iscoroutinefunction(ReportBulkUpsertAPIView.post)
```

- [ ] **Step 2.2: Run the new file and confirm the expected mixed-result baseline**

```bash
uv run cli test -- radis/reports/tests/test_report_api.py -v
```

Expected result:
- All endpoint tests (URL resolution, POST, GET, PUT, DELETE, bulk-upsert behavior) **PASS** — they run against the current DRF viewset which already implements this contract.
- The three async-shape guards (`test_report_list_post_is_coroutine`, `test_report_detail_methods_are_coroutines`, `test_report_bulk_upsert_post_is_coroutine`) **FAIL with `ModuleNotFoundError: No module named 'radis.reports.api.views'`**.

If any endpoint test fails, **stop and report** — that means the test does not actually match the existing contract and needs fixing before the rewrite.

- [ ] **Step 2.3: Commit**

```bash
git add radis/reports/tests/test_report_api.py
git commit -m "$(cat <<'EOF'
test(reports): add end-to-end report API tests + async-shape guards

Lock the wire-level contract for all five report endpoints before the
ADRF rewrite. The three iscoroutinefunction guards fail today and will
go green once the new ADRF view classes land.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 3: Convert `viewsets.py` from sync DRF to async ADRF

Rewrite `radis/reports/api/viewsets.py` in place. `ReportViewSet` keeps its name and module location but now subclasses `adrf.viewsets.GenericViewSet` + the four create / retrieve / update / destroy async mixins from `adrf.mixins`, plus an `@action` for `bulk_upsert`. The `bulk_upsert_reports` helper renamed in Task 1 stays in the same module — there is no `bulk.py`, no `views.py`, and no rename. The legacy module-level `from rest_framework import mixins, status, viewsets` imports are swapped for `from adrf import mixins as amixins; from adrf.viewsets import GenericViewSet`, and every mixin method override becomes `async def acreate` / `aretrieve` / `aupdate` / `adestroy`.

**Files:**
- Modify (rewrite): `radis/reports/api/viewsets.py`

- [ ] **Step 3.1: Rewrite `radis/reports/api/viewsets.py`**

```python
"""ADRF report viewset.

Single async ViewSet that mirrors the shape of the legacy DRF ReportViewSet:
GenericViewSet + selected adrf mixins, dispatched via DefaultRouter. Custom
behaviour is added by overriding the async mixin methods (acreate /
aretrieve / aupdate / adestroy) and the @action for bulk-upsert.

Strategy:
  - Native async ORM (`.aget`) for single-call lookups.
  - `channels.db.database_sync_to_async` for serializer + transaction blocks
    (DRF serializers and `transaction.atomic()` are sync-only).
  - Request body materialised on the async thread before entering any sync
    wrapper, so the ASGI body stream is never touched from a worker thread.
  - For mutating handlers, the ORM write and `transaction.on_commit`
    registration share one atomic block on the same DB connection so the
    callback is correctly bound to the write's transaction.

See the design doc at
docs/superpowers/specs/2026-06-08-adrf-report-views-design.md.
"""
import asyncio
import logging
from typing import Any

from adrf import mixins as amixins
from adrf.viewsets import GenericViewSet
from channels.db import database_sync_to_async
from django.db import transaction
from django.http import Http404
from rest_framework import status
from rest_framework.decorators import action
from rest_framework.exceptions import ValidationError
from rest_framework.permissions import IsAdminUser
from rest_framework.request import Request, clone_request
from rest_framework.response import Response

from ..models import Report
from ..site import (
    document_fetchers,
    reports_created_handlers,
    reports_deleted_handlers,
    reports_updated_handlers,
)
from .bulk import bulk_upsert_reports
from .serializers import ReportSerializer

logger = logging.getLogger(__name__)


class ReportViewSet(
    amixins.CreateModelMixin,
    amixins.RetrieveModelMixin,
    amixins.UpdateModelMixin,
    amixins.DestroyModelMixin,
    GenericViewSet,
):
    queryset = Report.objects.all()
    serializer_class = ReportSerializer
    lookup_field = "document_id"
    permission_classes = [IsAdminUser]
    # Block PATCH at the dispatcher level (returns 405). We never define
    # `partial_update` / `apartial_update` for the same effect.
    http_method_names = ["get", "post", "put", "delete", "head", "options"]

    async def acreate(self, request: Request, *args: Any, **kwargs: Any) -> Response:
        data = request.data

        @database_sync_to_async
        def _create() -> dict[str, Any]:
            serializer = self.get_serializer(data=data)
            serializer.is_valid(raise_exception=True)
            report = serializer.save()

            def on_commit():
                for handler in reports_created_handlers:
                    logger.debug(
                        f"{handler.name} - handle newly created reports: "
                        f"{[report.document_id]}"
                    )
                    handler.handle([report])

            transaction.on_commit(on_commit)
            return serializer.data

        return Response(await _create(), status=status.HTTP_201_CREATED)

    async def aretrieve(self, request: Request, *args: Any, **kwargs: Any) -> Response:
        try:
            report = await Report.objects.select_related("language").aget(
                document_id=kwargs[self.lookup_field]
            )
        except Report.DoesNotExist:
            raise Http404

        data = await database_sync_to_async(
            lambda: self.get_serializer(report).data
        )()

        full = request.GET.get("full", "").lower() in ("true", "1", "yes")
        if full:
            async def _fetch(fetcher):
                return fetcher.source, await database_sync_to_async(fetcher.fetch)(report)

            results = await asyncio.gather(
                *(_fetch(f) for f in document_fetchers.values())
            )
            data["documents"] = {
                source: doc for source, doc in results if doc is not None
            }

        return Response(data)

    async def aupdate(self, request: Request, *args: Any, **kwargs: Any) -> Response:
        document_id = kwargs[self.lookup_field]
        upsert = request.GET.get("upsert", "").lower() in ("true", "1", "yes")
        data = request.data

        try:
            report = await Report.objects.aget(document_id=document_id)
        except Report.DoesNotExist:
            report = None

        if report is None and not upsert:
            raise Http404
        if report is None and upsert:
            # Replicates DRF's `get_object_or_none` + `clone_request("POST")`
            # permission re-check: a non-staff PUT?upsert=true on a missing
            # id must come back as 403, not 404.
            await database_sync_to_async(self.check_permissions)(
                clone_request(request, "POST")
            )

        @database_sync_to_async
        def _save() -> tuple[dict[str, Any], int]:
            serializer = self.get_serializer(report, data=data)
            serializer.is_valid(raise_exception=True)
            saved = serializer.save()

            def on_commit():
                handlers = (
                    reports_created_handlers
                    if report is None
                    else reports_updated_handlers
                )
                event = "newly created" if report is None else "updated"
                for handler in handlers:
                    logger.debug(
                        f"{handler.name} - handle {event} reports: "
                        f"{[saved.document_id]}"
                    )
                    handler.handle([saved])

            transaction.on_commit(on_commit)
            return serializer.data, (
                status.HTTP_201_CREATED if report is None else status.HTTP_200_OK
            )

        body, http_status = await _save()
        return Response(body, status=http_status)

    async def adestroy(self, request: Request, *args: Any, **kwargs: Any) -> Response:
        try:
            report = await Report.objects.aget(document_id=kwargs[self.lookup_field])
        except Report.DoesNotExist:
            raise Http404

        @database_sync_to_async
        def _delete_and_schedule() -> None:
            with transaction.atomic():
                report.delete()

                def on_commit():
                    for handler in reports_deleted_handlers:
                        logger.debug(
                            f"{handler.name} - handle deleted report: "
                            f"{report.document_id}"
                        )
                        handler.handle([report])

                transaction.on_commit(on_commit)

        await _delete_and_schedule()
        return Response(status=status.HTTP_204_NO_CONTENT)

    @action(detail=False, methods=["post"], url_path="bulk-upsert")
    async def bulk_upsert(self, request: Request) -> Response:
        payloads = request.data
        if not isinstance(payloads, list):
            return Response(
                {"detail": "Expected a list of report objects."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        replace = request.GET.get("replace", "true").lower() in ("true", "1", "yes")
        if not replace:
            return Response(
                {
                    "detail": (
                        "replace=false is not supported for bulk upsert. "
                        "Use replace=true."
                    )
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        @database_sync_to_async
        def _do() -> dict[str, Any]:
            valid_payloads: list[dict[str, Any]] = []
            errors: list[dict[str, Any]] = []
            for index, payload in enumerate(payloads):
                serializer = self.get_serializer(
                    data=payload,
                    context={
                        **self.get_serializer_context(),
                        "skip_document_id_unique": True,
                    },
                )
                try:
                    serializer.is_valid(raise_exception=True)
                except ValidationError as exc:
                    document_id = (
                        payload.get("document_id")
                        if isinstance(payload, dict)
                        else None
                    )
                    logger.error(
                        "Bulk upsert validation failed (index=%s document_id=%s): %s",
                        index, document_id, exc.detail,
                    )
                    errors.append({
                        "index": index,
                        "document_id": document_id,
                        "errors": exc.detail,
                    })
                    continue
                valid_payloads.append(serializer.validated_data)

            created_ids: list[str] = []
            updated_ids: list[str] = []
            if valid_payloads:
                created_ids, updated_ids = bulk_upsert_reports(valid_payloads)

            body: dict[str, Any] = {
                "created": len(created_ids),
                "updated": len(updated_ids),
                "invalid": len(errors),
            }
            if errors:
                max_errors = 50
                body["errors"] = errors[:max_errors]
                body["errors_truncated"] = len(errors) > max_errors
            return body

        return Response(await _do())
```

- [ ] **Step 3.2: Update the async-shape guard tests in `radis/reports/tests/test_report_api.py`**

The three guards from Task 2 (which currently look up `ReportListAPIView`, `ReportDetailAPIView`, `ReportBulkUpsertAPIView`) need to point at the viewset's async methods:

```python
def test_report_viewset_methods_are_coroutines():
    views = importlib.import_module("radis.reports.api.views")
    vs = views.ReportViewSet
    for name in ("acreate", "aretrieve", "aupdate", "adestroy", "bulk_upsert"):
        assert inspect.iscoroutinefunction(getattr(vs, name)), f"{name} is not async"
```

Replace the previous `test_report_list_post_is_coroutine`, `test_report_detail_methods_are_coroutines`, and `test_report_bulk_upsert_post_is_coroutine` with this single test.

- [ ] **Step 3.3: Lint and commit**

```bash
uv run cli lint
git add radis/reports/api/views.py radis/reports/tests/test_report_api.py
git commit -m "feat(reports): add ReportViewSet (not yet wired into urls)"
```

---

## Task 4: Sanity-check `urls.py` and run the report tests

The URL config in `radis/reports/api/urls.py` already registers `ReportViewSet` on a `DefaultRouter`. Since Task 3 rewrites `viewsets.py` in place (no rename, no new module), the import in `urls.py` (`from .viewsets import ReportViewSet`) does not change. This task is essentially a verification pass.

**Files:**
- Read-only: `radis/reports/api/urls.py`

- [ ] **Step 4.1: Confirm `urls.py` contents**

```python
from adrf.routers import DefaultRouter
from django.urls import include, path

from .viewsets import ReportViewSet

router = DefaultRouter()
router.register("", ReportViewSet, basename="report")

urlpatterns = [
    path("", include(router.urls)),
]
```

Important: use `adrf.routers.DefaultRouter`, **not** `rest_framework.routers.DefaultRouter`. DRF's router maps HTTP methods to sync action names (`create`/`retrieve`/`update`/`destroy`), which `adrf.mixins.*` inherit from DRF's sync mixins — so dispatch would silently call the inherited sync methods instead of our async overrides. ADRF's router remaps to `acreate`/`aretrieve`/`aupdate`/`adestroy` when `view_is_async=True`.

The router auto-generates the same URL patterns and names the legacy code emitted:

| Pattern | Method(s) | Viewset method | Route name |
| --- | --- | --- | --- |
| `/api/reports/` | POST | `acreate` | `report-list` |
| `/api/reports/bulk-upsert/` | POST | `bulk_upsert` (the `@action`) | `report-bulk-upsert` |
| `/api/reports/{document_id}/` | GET/PUT/DELETE | `aretrieve` / `aupdate` / `adestroy` | `report-detail` |

`lookup_value_regex` defaults to `[^/.]+`, which forbids `.` in `document_id` — the legacy behaviour.

- [ ] **Step 4.2: Run the report test files**

```bash
uv run cli test -- radis/reports/tests/test_report_api.py -v
uv run cli test -- radis/reports/tests/test_bulk_upsert.py -v
```

Expected: all tests pass.

---

## Task 5: Pre-PR verification

No code changes — just confirm the project is healthy end-to-end before opening the PR.

- [ ] **Step 5.1: Lint**

```bash
uv run cli lint
```

Expected: zero issues. If anything fails, fix it (likely import ordering or unused imports — leftover `from rest_framework import ...` in unrelated files won't be touched).

- [ ] **Step 5.2: Full test suite**

```bash
uv run cli test
```

Expected: full green. Pay attention to any failure outside the reports app — that signals an unintended coupling we missed.

- [ ] **Step 5.3: Manual smoke test against the running stack**

The dev stack should still be up (`uv run cli compose-up -d` from prereqs). Use a fresh token to confirm each endpoint at the wire level:

```bash
# Create an admin user + token in the running container if you don't have one:
uv run cli shell <<'PY'
from adit_radis_shared.accounts.factories import UserFactory, GroupFactory
from adit_radis_shared.token_authentication.models import Token
user = UserFactory.create(is_staff=True, is_active=True)
group = GroupFactory.create()
user.groups.add(group)
_, token = Token.objects.create_token(user, "smoke test", None)
print(f"TOKEN={token}")
print(f"GROUP_ID={group.pk}")
PY
```

Then exercise each endpoint:

```bash
export TOKEN=<from above>
export GROUP=<from above>
BASE=http://localhost:8000/api/reports

# CREATE
curl -sf -X POST "$BASE/" \
  -H "Authorization: Token $TOKEN" -H "Content-Type: application/json" \
  -d "$(cat <<JSON
{"document_id":"SMOKE-1","language":"en","groups":[$GROUP],
 "pacs_aet":"PACS","pacs_name":"P","pacs_link":"",
 "patient_id":"P1","patient_birth_date":"1980-01-01","patient_sex":"M",
 "study_description":"S","study_datetime":"2024-01-01T00:00:00Z",
 "study_instance_uid":"1.2.3","accession_number":"A1",
 "modalities":["CT"],"metadata":{"k":"v"},"body":"hello"}
JSON
)" | python -m json.tool

# RETRIEVE
curl -sf "$BASE/SMOKE-1/" -H "Authorization: Token $TOKEN" | python -m json.tool

# UPDATE
curl -sf -X PUT "$BASE/SMOKE-1/" \
  -H "Authorization: Token $TOKEN" -H "Content-Type: application/json" \
  -d "$(cat <<JSON
{"document_id":"SMOKE-1","language":"en","groups":[$GROUP],
 "pacs_aet":"PACS","pacs_name":"P","pacs_link":"",
 "patient_id":"P1","patient_birth_date":"1980-01-01","patient_sex":"M",
 "study_description":"S","study_datetime":"2024-01-01T00:00:00Z",
 "study_instance_uid":"1.2.3","accession_number":"A1",
 "modalities":["CT"],"metadata":{"k":"v"},"body":"updated"}
JSON
)" | python -m json.tool

# PATCH → 405
curl -s -o /dev/null -w "%{http_code}\n" -X PATCH "$BASE/SMOKE-1/" \
  -H "Authorization: Token $TOKEN"   # expect 405

# BULK UPSERT
curl -sf -X POST "$BASE/bulk-upsert/" \
  -H "Authorization: Token $TOKEN" -H "Content-Type: application/json" \
  -d "[$(cat <<JSON
{"document_id":"SMOKE-BULK","language":"en","groups":[$GROUP],
 "pacs_aet":"PACS","pacs_name":"P","pacs_link":"",
 "patient_id":"P2","patient_birth_date":"1981-01-01","patient_sex":"F",
 "study_description":"S","study_datetime":"2024-02-01T00:00:00Z",
 "study_instance_uid":"2.3.4","accession_number":"A2",
 "modalities":["MR"],"metadata":{},"body":"bulk"}
JSON
)]" | python -m json.tool   # expect {"created":1,"updated":0,"invalid":0}

# DELETE
curl -s -o /dev/null -w "%{http_code}\n" -X DELETE "$BASE/SMOKE-1/" \
  -H "Authorization: Token $TOKEN"   # expect 204
```

Expected: 201, 200, 200, 405, 200 (with the right counts), 204.

- [ ] **Step 5.4: Open the PR**

```bash
git push -u origin feat/adrf-views
gh pr create --title "Convert report API to ADRF views (prep for async embedding trigger)" --body "$(cat <<'EOF'
## Summary
- Replace the sync DRF `ReportViewSet` + `DefaultRouter` with three explicit `adrf.views.APIView` subclasses (`ReportListAPIView`, `ReportDetailAPIView`, `ReportBulkUpsertAPIView`) wired into `urls.py` via `path()` — same pattern as ADIT's `dicom_web/views.py`.
- Rename `_bulk_upsert_reports` → `bulk_upsert_reports` inside `viewsets.py` (no file split).
- Use native async ORM (`.aget`, `.adelete`) for simple lookups and `channels.db.database_sync_to_async` to wrap DRF serializer + `transaction.atomic()` blocks. `ReportSerializer` itself is untouched.

**API contract is byte-for-byte unchanged** — URLs, response shapes, status codes, query params (`?upsert`, `?full`, `?replace`), and the 405-for-PATCH behavior are all preserved. Locked in by the new end-to-end tests in `radis/reports/tests/test_report_api.py` (which run against both old and new implementations during the rewrite).

This PR does **not** wire the async embedding enqueue into the upload path — that is the follow-up. The existing periodic `embedding_launcher` continues to be the sole trigger today.

## Test plan
- [x] `uv run cli lint`
- [x] `uv run cli test` (full suite green)
- [x] Manual smoke: `curl` each endpoint end-to-end (create / retrieve / update / patch→405 / bulk-upsert / delete)

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

---

## Out of scope reminders

- No async embedding trigger from the request path (follow-up PR).
- No serializer refactor — `ReportSerializer` stays sync.
- No other API surfaces touched (`radis.search`, `radis.chats`, `radis.extractions`, etc.).
- No migrations, settings, or env-var changes.
