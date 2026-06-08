"""ADRF report viewset.

Single async ViewSet that mirrors the shape of the legacy DRF ReportViewSet:
GenericViewSet + selected adrf mixins, dispatched via DefaultRouter. Custom
behaviour is added by overriding the async mixin methods (acreate /
aretrieve / aupdate / adestroy) and the @action for bulk-upsert.

Note on async/sync hygiene: the `adrf.mixins` inherit from DRF's sync
mixins, so this class technically has sync `create`/`retrieve`/`update`/
`destroy` siblings on the MRO. ADRF's `view_is_async` flips the dispatcher
to the async path whenever any method on the class is a coroutine, so as
long as our overrides stay `async def`, the sync siblings are never
reached. The async-shape guard tests in test_report_api.py pin every
entry point to `inspect.iscoroutinefunction` to catch any accidental
sync override.

Strategy:
  - Native async ORM (`.aget`) for single-call lookups.
  - `channels.db.database_sync_to_async` for serializer + transaction blocks
    (DRF serializers and `transaction.atomic()` are sync-only).
  - Request body (`request.data`) is materialised on the async thread
    before entering any sync wrapper, so the ASGI body stream is never
    touched from a worker thread.
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

    # DRF's `@action` stub types its callable argument as a sync view returning
    # HttpResponseBase, but ADRF's dispatcher handles `async def` actions just
    # fine (the @action decorator only attaches routing metadata). Narrow
    # suppression of a stub-only mismatch:
    @action(detail=False, methods=["post"], url_path="bulk-upsert")  # pyright: ignore[reportArgumentType]
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
                        index,
                        document_id,
                        exc.detail,
                    )
                    errors.append(
                        {
                            "index": index,
                            "document_id": document_id,
                            "errors": exc.detail,
                        }
                    )
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
