# radis/reports/api/views.py
"""ADRF report views.

Three async APIViews mirroring what `ReportViewSet` did before:

  - `ReportListAPIView`  — POST /api/reports/
  - `ReportDetailAPIView` — GET/PUT/DELETE /api/reports/{document_id}/
  - `ReportBulkUpsertAPIView` — POST /api/reports/bulk-upsert/

Strategy:
  - Native async ORM (`.aget`, `.adelete`) for single-call lookups.
  - `channels.db.database_sync_to_async` for serializer + transaction blocks,
    which must stay synchronous (DRF serializers, `transaction.atomic()`).
  - `transaction.on_commit` callbacks fire from inside the wrapped sync
    block, preserving today's "after commit" semantics for created /
    updated / deleted handlers.

See the design doc at
docs/superpowers/specs/2026-06-08-adrf-report-views-design.md.
"""
import logging
from typing import Any

from adrf.views import APIView as AsyncApiView
from channels.db import database_sync_to_async
from django.db import transaction
from django.http import Http404
from rest_framework import status
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


class ReportListAPIView(AsyncApiView):
    permission_classes = [IsAdminUser]

    async def post(self, request: Request) -> Response:
        @database_sync_to_async
        def _create() -> dict[str, Any]:
            serializer = ReportSerializer(
                data=request.data, context={"request": request}
            )
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

        data = await _create()
        return Response(data, status=status.HTTP_201_CREATED)


class ReportDetailAPIView(AsyncApiView):
    permission_classes = [IsAdminUser]

    async def get(self, request: Request, document_id: str) -> Response:
        try:
            report = await Report.objects.select_related("language").aget(
                document_id=document_id
            )
        except Report.DoesNotExist:
            raise Http404

        data = await database_sync_to_async(
            lambda: ReportSerializer(report, context={"request": request}).data
        )()

        full = request.GET.get("full", "").lower() in ("true", "1", "yes")
        if full:
            documents: dict[str, Any] = {}
            for fetcher in document_fetchers.values():
                doc = await database_sync_to_async(fetcher.fetch)(report)
                if doc is not None:
                    documents[fetcher.source] = doc
            data["documents"] = documents

        return Response(data)

    async def put(self, request: Request, document_id: str) -> Response:
        upsert = request.GET.get("upsert", "").lower() in ("true", "1", "yes")

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
            serializer = ReportSerializer(
                report, data=request.data, context={"request": request}
            )
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

        data, http_status = await _save()
        return Response(data, status=http_status)

    async def delete(self, request: Request, document_id: str) -> Response:
        try:
            report = await Report.objects.aget(document_id=document_id)
        except Report.DoesNotExist:
            raise Http404

        await report.adelete()

        @database_sync_to_async
        def _schedule_handlers() -> None:
            def on_commit():
                for handler in reports_deleted_handlers:
                    logger.debug(
                        f"{handler.name} - handle deleted report: "
                        f"{report.document_id}"
                    )
                    handler.handle([report])

            transaction.on_commit(on_commit)

        await _schedule_handlers()
        return Response(status=status.HTTP_204_NO_CONTENT)


class ReportBulkUpsertAPIView(AsyncApiView):
    permission_classes = [IsAdminUser]

    async def post(self, request: Request) -> Response:
        if not isinstance(request.data, list):
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
            for index, payload in enumerate(request.data):
                serializer = ReportSerializer(
                    data=payload,
                    context={
                        "request": request,
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
