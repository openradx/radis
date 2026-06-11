"""ADRF report viewset.

1:1 async conversion of the legacy DRF `ReportViewSet`: same mixin
lineup (now from `adrf.mixins`), same `GenericViewSet` base, routed via
`adrf.routers.DefaultRouter` so the router maps HTTP methods to the
async action names (`acreate` / `aretrieve` / `aupdate` / `adestroy`).
Per-handler architectural notes live at each method.

Sync-mixin trap: `adrf.mixins.*ModelMixin` inherits from DRF's sync
mixins, so the class has both sync `create` and async `acreate` (etc.)
on the MRO. The async-shape guard in test_report_api.py pins every
dispatched method to `iscoroutinefunction` to catch a future contributor
accidentally overriding the sync sibling. That guard cannot catch a
mis-wired router — `adrf.routers.DefaultRouter` is part of the contract.

PATCH is blocked at the dispatcher level via `http_method_names`; we
never define `partial_update` / `apartial_update`.
"""
import asyncio
import logging
from typing import Any, cast

from adrf import mixins as amixins
from adrf.viewsets import GenericViewSet
from asgiref.sync import async_to_sync, sync_to_async
from django.conf import settings
from django.db import transaction
from django.http import Http404
from django.utils import timezone
from rest_framework import status
from rest_framework.decorators import action
from rest_framework.exceptions import ValidationError
from rest_framework.permissions import IsAdminUser
from rest_framework.request import Request, clone_request
from rest_framework.response import Response

from radis.pgsearch.tasks import enqueue_bulk_index_reports
from radis.pgsearch.utils.indexing import bulk_upsert_report_search_vectors

from ..models import Language, Metadata, Modality, Report
from ..site import (
    document_fetchers,
    reports_created_handlers,
    reports_deleted_handlers,
    reports_updated_handlers,
)
from . import operations
from .serializers import ReportSerializer

logger = logging.getLogger(__name__)


async def bulk_upsert_reports(
    validated_reports: list[dict[str, Any]],
) -> tuple[list[str], list[str]]:
    """Bulk-upsert validated report payloads.

    Four phases:
      1. Dedupe input by document_id  (CPU,    `@sync_to_async` helper)
      2. Preflight Language/Modality/existing-Report reads  (native async ORM)
      3. Build new_reports / updated_reports lists  (CPU, `@sync_to_async` helper)
      4. Atomic writes  (`@sync_to_async @transaction.atomic` helper, inline
         sync ORM since the writes are single-statement bulk ops that don't
         decompose into per-entity operations)

    Phase 1 / 3 run off the event loop so the CPU loops don't block other
    requests. Phase 2 uses native async ORM — but note that as of Django
    6.0/6.1 every `a*` method is internally `sync_to_async`-wrapped, so
    those calls dispatch to the asgiref thread pool just like our explicit
    `sync_to_async` calls. The win today is architectural clarity; once a
    native async DB backend ships, Phase 2 (and Phase 4's atomic helper
    + the serializer/adestroy helpers) collapse to `async with async_atomic():`
    + direct `await operations.X(...)`.
    """
    if not validated_reports:
        return [], []

    report_field_names = (
        "document_id",
        "pacs_aet",
        "pacs_name",
        "pacs_link",
        "patient_id",
        "patient_birth_date",
        "patient_sex",
        "study_description",
        "study_datetime",
        "study_instance_uid",
        "accession_number",
        "body",
    )

    # ── Phase 1: CPU-only dedupe of incoming payload (off-loop) ──
    @sync_to_async(thread_sensitive=True)
    def _dedupe_payload() -> list[dict[str, Any]]:
        deduped_reports: dict[str, dict[str, Any]] = {}
        duplicate_count = 0
        for report in validated_reports:
            document_id = report["document_id"]
            if document_id in deduped_reports:
                duplicate_count += 1
            deduped_reports[document_id] = report
        if duplicate_count:
            logger.warning(
                "Bulk upsert payload contained %s duplicate document_ids; "
                "keeping last occurrence.",
                duplicate_count,
            )
            return list(deduped_reports.values())
        return validated_reports

    validated_reports = await _dedupe_payload()
    document_ids = [report["document_id"] for report in validated_reports]

    # ── Phase 2: preflight reads/writes that do NOT need atomicity ──
    language_codes = {report["language"]["code"] for report in validated_reports}
    language_by_code = {
        lang.code: lang
        async for lang in Language.objects.filter(code__in=language_codes)
    }
    missing_language_codes = language_codes - language_by_code.keys()
    if missing_language_codes:
        await Language.objects.abulk_create(
            [Language(code=code) for code in missing_language_codes],
            ignore_conflicts=True,
            batch_size=settings.REPORTS_BULK_DB_BATCH_SIZE,
        )
        language_by_code = {
            lang.code: lang
            async for lang in Language.objects.filter(code__in=language_codes)
        }

    modality_codes = {
        modality["code"]
        for report in validated_reports
        for modality in report.get("modalities", [])
    }
    modality_by_code = {
        mod.code: mod
        async for mod in Modality.objects.filter(code__in=modality_codes)
    }
    missing_modality_codes = modality_codes - modality_by_code.keys()
    if missing_modality_codes:
        await Modality.objects.abulk_create(
            [Modality(code=code) for code in missing_modality_codes],
            ignore_conflicts=True,
            batch_size=settings.REPORTS_BULK_DB_BATCH_SIZE,
        )
        modality_by_code = {
            mod.code: mod
            async for mod in Modality.objects.filter(code__in=modality_codes)
        }

    existing_by_document_id = {
        report.document_id: report
        async for report in Report.objects.filter(document_id__in=document_ids)
    }

    # ── Phase 3: CPU-only build of new_reports / updated_reports lists (off-loop) ──
    @sync_to_async(thread_sensitive=True)
    def _build_report_lists() -> tuple[
        list[Report], list[Report], list[str], list[str]
    ]:
        now = timezone.now()
        created_ids: list[str] = []
        updated_ids: list[str] = []
        new_reports: list[Report] = []
        updated_reports: list[Report] = []

        for report_data in validated_reports:
            document_id = report_data["document_id"]
            language = language_by_code[report_data["language"]["code"]]
            report_fields = {field: report_data[field] for field in report_field_names}

            existing = existing_by_document_id.get(document_id)
            if existing:
                for field, value in report_fields.items():
                    setattr(existing, field, value)
                existing.language = language
                existing.updated_at = now
                updated_reports.append(existing)
                updated_ids.append(document_id)
            else:
                new_reports.append(
                    Report(
                        **report_fields,
                        language=language,
                        created_at=now,
                        updated_at=now,
                    )
                )
                created_ids.append(document_id)

        return new_reports, updated_reports, created_ids, updated_ids

    new_reports, updated_reports, created_ids, updated_ids = await _build_report_lists()

    # ── Phase 4: atomic writes ──
    @sync_to_async(thread_sensitive=True)
    @transaction.atomic
    def _do_atomic_writes() -> None:
        def _dedupe_by_key(
            items: list[dict[str, Any]], key_name: str
        ) -> tuple[list[dict[str, Any]], int]:
            if not items:
                return [], 0
            by_key: dict[str, dict[str, Any]] = {}
            for item in items:
                by_key[item[key_name]] = item
            return list(by_key.values()), len(items) - len(by_key)

        def _dedupe_metadata(
            items: list[dict[str, Any]]
        ) -> tuple[list[dict[str, Any]], int]:
            if not items:
                return [], 0
            by_key: dict[str, dict[str, Any]] = {}
            duplicates = 0
            for item in items:
                key = item["key"]
                if key in by_key:
                    duplicates += 1
                by_key[key] = item
            return list(by_key.values()), duplicates

        def _dedupe_groups(items: list[Any]) -> tuple[list[int], int]:
            if not items:
                return [], 0
            by_id: dict[int, int] = {}
            for group in items:
                group_id = int(getattr(group, "pk", group))
                by_id[group_id] = group_id
            return list(by_id.values()), len(items) - len(by_id)


        if new_reports:
            Report.objects.bulk_create(new_reports, batch_size=settings.REPORTS_BULK_DB_BATCH_SIZE)
        if updated_reports:
            Report.objects.bulk_update(
                updated_reports,
                fields=[*report_field_names, "language", "updated_at"],
                batch_size=settings.REPORTS_BULK_DB_BATCH_SIZE,
            )

        report_id_by_document_id = {
            report.document_id: report.pk
            for report in Report.objects.filter(document_id__in=document_ids).only(
                "id", "document_id"
            )
        }
        report_ids = list(report_id_by_document_id.values())

        if report_ids:
            Metadata.objects.filter(report_id__in=report_ids).delete()

            metadata_rows: list[Metadata] = []
            metadata_duplicate_count = 0
            for report_data in validated_reports:
                report_id = report_id_by_document_id[report_data["document_id"]]
                metadata_items, duplicates = _dedupe_metadata(report_data.get("metadata", []))
                metadata_duplicate_count += duplicates
                for item in metadata_items:
                    metadata_rows.append(
                        Metadata(report_id=report_id, key=item["key"], value=item["value"])
                    )
            if metadata_rows:
                Metadata.objects.bulk_create(
                    metadata_rows, batch_size=settings.REPORTS_BULK_DB_BATCH_SIZE
                )

            modality_through = Report.modalities.through
            modality_through.objects.filter(report_id__in=report_ids).delete()

            modality_rows = []
            modality_duplicate_count = 0
            for report_data in validated_reports:
                report_id = report_id_by_document_id[report_data["document_id"]]
                modality_items, duplicates = _dedupe_by_key(
                    report_data.get("modalities", []), "code"
                )
                modality_duplicate_count += duplicates
                for modality in modality_items:
                    modality_id = modality_by_code[modality["code"]].pk
                    modality_rows.append(
                        modality_through(report_id=report_id, modality_id=modality_id)
                    )
            if modality_rows:
                modality_through.objects.bulk_create(
                    modality_rows, batch_size=settings.REPORTS_BULK_DB_BATCH_SIZE
                )

            group_through = Report.groups.through
            group_through.objects.filter(report_id__in=report_ids).delete()

            group_rows = []
            group_duplicate_count = 0
            for report_data in validated_reports:
                report_id = report_id_by_document_id[report_data["document_id"]]
                group_items, duplicates = _dedupe_groups(report_data.get("groups", []))
                group_duplicate_count += duplicates
                for group_id in group_items:
                    group_rows.append(group_through(report_id=report_id, group_id=group_id))
            if group_rows:
                group_through.objects.bulk_create(
                    group_rows, batch_size=settings.REPORTS_BULK_DB_BATCH_SIZE
                )

            if metadata_duplicate_count or modality_duplicate_count or group_duplicate_count:
                logger.warning(
                    "Bulk upsert payload contained duplicate metadata/modality/group entries "
                    "(metadata=%s modalities=%s groups=%s); duplicates were dropped.",
                    metadata_duplicate_count,
                    modality_duplicate_count,
                    group_duplicate_count,
                )

        touched_report_ids = [
            report_id_by_document_id[document_id]
            for document_id in [*created_ids, *updated_ids]
            if document_id in report_id_by_document_id
        ]

        def on_commit():
            if created_ids:
                created_reports = list(Report.objects.filter(document_id__in=created_ids))
                for handler in reports_created_handlers:
                    handler.handle(created_reports)
            if updated_ids:
                updated_reports_after_commit = list(
                    Report.objects.filter(document_id__in=updated_ids)
                )
                for handler in reports_updated_handlers:
                    handler.handle(updated_reports_after_commit)
            if touched_report_ids:
                if settings.PGSEARCH_SYNC_INDEXING:
                    bulk_upsert_report_search_vectors(touched_report_ids)
                else:
                    enqueue_bulk_index_reports(touched_report_ids)

        transaction.on_commit(on_commit)

    await _do_atomic_writes()
    return created_ids, updated_ids


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
        serializer = cast(ReportSerializer, self.get_serializer(data=request.data))
        # `is_valid` is sync (DRF has no `ais_valid`) and hits the DB for
        # the `groups` PrimaryKeyRelatedField validator. Run it via
        # `sync_to_async` so we don't trip Django's async-unsafe guard.
        await sync_to_async(serializer.is_valid, thread_sensitive=True)(
            raise_exception=True
        )

        # `asave` owns its own `@transaction.atomic` block (inside
        # `ReportSerializer.acreate`). The atomic commits before `asave`
        # returns, so on_commit registered below fires immediately under
        # no outer transaction (production) or is captured by the test
        # fixture (`django_capture_on_commit_callbacks`).
        report = await serializer.asave()

        def on_commit():
            for handler in reports_created_handlers:
                logger.debug(
                    f"{handler.name} - handle newly created reports: "
                    f"{[report.document_id]}"
                )
                handler.handle([report])

        transaction.on_commit(on_commit)

        # `serializer.data` walks the model's related fields synchronously
        # (FK/M2M access). Wrap in `sync_to_async` for the same reason as
        # `is_valid`.
        response_data = await sync_to_async(
            lambda: serializer.data, thread_sensitive=True
        )()
        return Response(response_data, status=status.HTTP_201_CREATED)

    async def aretrieve(self, request: Request, *args: Any, **kwargs: Any) -> Response:
        try:
            report = await Report.objects.select_related("language").aget(
                document_id=kwargs[self.lookup_field]
            )
        except Report.DoesNotExist:
            raise Http404

        data = await sync_to_async(
            lambda: self.get_serializer(report).data,
            thread_sensitive=True,
        )()

        full = request.GET.get("full", "").lower() in ("true", "1", "yes")
        if full:
            async def _fetch(fetcher):
                return fetcher.source, await sync_to_async(
                    fetcher.fetch, thread_sensitive=True
                )(report)

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
            await sync_to_async(self.check_permissions, thread_sensitive=True)(
                clone_request(request, "POST")
            )

        serializer = cast(
            ReportSerializer, self.get_serializer(report, data=data)
        )
        await sync_to_async(serializer.is_valid, thread_sensitive=True)(
            raise_exception=True
        )

        # `asave` dispatches to `acreate` (if `report is None`) or
        # `aupdate` (otherwise); both own a `@transaction.atomic` block
        # internally.
        saved = await serializer.asave()

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

        response_data = await sync_to_async(
            lambda: serializer.data, thread_sensitive=True
        )()
        http_status = (
            status.HTTP_201_CREATED if report is None else status.HTTP_200_OK
        )
        return Response(response_data, status=http_status)

    async def adestroy(self, request: Request, *args: Any, **kwargs: Any) -> Response:
        try:
            report = await Report.objects.aget(document_id=kwargs[self.lookup_field])
        except Report.DoesNotExist:
            raise Http404

        # No serializer involved here, so adestroy owns the atomic helper
        # directly (instead of delegating it to a serializer like acreate /
        # aupdate do). The helper holds the transaction across the delete
        # and the `transaction.on_commit` registration so the callback is
        # correctly bound to the delete's transaction.
        @sync_to_async(thread_sensitive=True)
        @transaction.atomic
        def _delete_and_schedule() -> None:
            async_to_sync(operations.delete_report)(report)

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

        # Per-payload DRF serializer validation is sync (DRF has no async
        # `ais_valid`). No atomicity needed — validators only read.
        @sync_to_async(thread_sensitive=True)
        def _validate() -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
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
            return valid_payloads, errors

        valid_payloads, errors = await _validate()

        created_ids: list[str] = []
        updated_ids: list[str] = []
        if valid_payloads:
            created_ids, updated_ids = await bulk_upsert_reports(valid_payloads)

        body: dict[str, Any] = {
            "created": len(created_ids),
            "updated": len(updated_ids),
            "invalid": len(errors),
        }
        if errors:
            max_errors = 50
            body["errors"] = errors[:max_errors]
            body["errors_truncated"] = len(errors) > max_errors
        return Response(body)
