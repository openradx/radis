# radis/reports/api/views.py
"""ADRF report views.

Three async APIViews mirroring what `ReportViewSet` did before:

  - `ReportListAPIView`  — POST /api/reports/
  - `ReportDetailAPIView` — GET/PUT/DELETE /api/reports/{document_id}/
  - `ReportBulkUpsertAPIView` — POST /api/reports/bulk-upsert/

Strategy:
  - Native async ORM (`.aget`) for single-call lookups; `asyncio.gather`
    to parallelize independent async work (document fetchers).
  - `channels.db.database_sync_to_async` for serializer + transaction blocks,
    which must stay synchronous (DRF serializers, `transaction.atomic()`).
  - Request body (`request.data`) is materialized on the async thread before
    entering any sync wrapper, so the ASGI body stream is never touched
    from a worker thread.
  - For mutating handlers, the ORM write and `transaction.on_commit`
    registration share one atomic block on the same DB connection so the
    callback is correctly bound to the write's transaction.

See the design doc at
docs/superpowers/specs/2026-06-08-adrf-report-views-design.md.
"""
import asyncio
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

from radis.pgsearch.utils.inline_embedding import embed_reports_inline

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
        data = request.data

        @database_sync_to_async
        def _create() -> tuple[dict[str, Any], int]:
            serializer = ReportSerializer(
                data=data, context={"request": request}
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
            return serializer.data, report.pk

        body, report_pk = await _create()
        await embed_reports_inline([report_pk])
        return Response(body, status=status.HTTP_201_CREATED)


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
            async def _fetch(fetcher):
                return fetcher.source, await database_sync_to_async(fetcher.fetch)(report)

            results = await asyncio.gather(
                *(_fetch(f) for f in document_fetchers.values())
            )
            data["documents"] = {
                source: doc for source, doc in results if doc is not None
            }

        return Response(data)

    async def put(self, request: Request, document_id: str) -> Response:
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
        def _save() -> tuple[dict[str, Any], int, int]:
            serializer = ReportSerializer(
                report, data=data, context={"request": request}
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
            return (
                serializer.data,
                status.HTTP_201_CREATED if report is None else status.HTTP_200_OK,
                saved.pk,
            )

        body, http_status, saved_pk = await _save()
        await embed_reports_inline([saved_pk])
        return Response(body, status=http_status)

    async def delete(self, request: Request, document_id: str) -> Response:
        try:
            report = await Report.objects.aget(document_id=document_id)
        except Report.DoesNotExist:
            raise Http404

        @database_sync_to_async
        def _delete_and_schedule() -> None:
            # Run delete and on_commit registration in one atomic block on
            # the same sync connection so the callback is correctly bound
            # to the delete's transaction (Gemini PR #230 review fix).
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


class ReportBulkUpsertAPIView(AsyncApiView):
    permission_classes = [IsAdminUser]

    async def post(self, request: Request) -> Response:
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
        def _do() -> tuple[dict[str, Any], list[int]]:
            valid_payloads: list[dict[str, Any]] = []
            errors: list[dict[str, Any]] = []
            for index, payload in enumerate(payloads):
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

            touched_document_ids = [*created_ids, *updated_ids]
            touched_report_pks: list[int] = (
                list(
                    Report.objects.filter(document_id__in=touched_document_ids).values_list(
                        "pk", flat=True
                    )
                )
                if touched_document_ids
                else []
            )

            body: dict[str, Any] = {
                "created": len(created_ids),
                "updated": len(updated_ids),
                "invalid": len(errors),
            }
            if errors:
                max_errors = 50
                body["errors"] = errors[:max_errors]
                body["errors_truncated"] = len(errors) > max_errors
            return body, touched_report_pks

        body, touched_report_pks = await _do()
        await embed_reports_inline(touched_report_pks)
        return Response(body)
