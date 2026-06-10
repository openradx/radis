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

  - Native async ORM (`aget`, `async for` comprehensions, `abulk_create`,
    `aexists`, ...) for everything that does NOT need atomicity.
  - Sync helper closure per handler, decorated with
    `@sync_to_async(thread_sensitive=True)`. The wrapper schedules the
    sync body on the asgiref thread pool. `thread_sensitive=True` is
    required so the sync helper always runs on Django's shared sync
    thread; without it, each call would land on a fresh thread with its
    own DB connection, breaking transaction semantics.
  - Stack `@transaction.atomic` *on top of* the sync_to_async decorator
    ONLY when the helper itself needs to own a transaction — i.e. when
    it issues multiple writes that must commit together and/or registers
    `transaction.on_commit` callbacks whose binding to that write must
    be guaranteed. Concretely: `_do_atomic_writes` inside
    `bulk_upsert_reports` (multi-table churn) and
    `_delete_and_schedule` inside `adestroy` (delete + on_commit
    binding). The `acreate` and `aupdate` helpers do NOT get
    `@transaction.atomic` because `ReportSerializer.create` /
    `ReportSerializer.update` already open their own atomic block for
    the multi-step write.

  - Note that even Django's native async ORM methods (`aget`,
    `abulk_create`, `aget_or_create`, ...) currently just wrap the sync
    method in `sync_to_async` internally — there is no native async DB
    backend in Django 6.0/6.1 (see PR #17275, stale since 2024). The
    `async for` / `await` calls in Phases 1–3 below therefore don't run
    in true parallel with the atomic block; they run on the asgiref
    thread pool just like our explicit `sync_to_async` calls. The win is
    purely architectural clarity: each function reads as "this is the
    async coordination, this one helper is sync because it owns the
    transaction".
"""
import asyncio
import logging
from typing import Any

from adrf import mixins as amixins
from adrf.viewsets import GenericViewSet
from asgiref.sync import sync_to_async
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
from .serializers import ReportSerializer

logger = logging.getLogger(__name__)

BULK_DB_BATCH_SIZE = 1000


async def bulk_upsert_reports(
    validated_reports: list[dict[str, Any]],
) -> tuple[list[str], list[str]]:
    if not validated_reports:
        return [], []

    # ── Phase 1: CPU-only dedupe of incoming payload ──
    deduped_reports: dict[str, dict[str, Any]] = {}
    duplicate_count = 0
    for report in validated_reports:
        document_id = report["document_id"]
        if document_id in deduped_reports:
            duplicate_count += 1
        deduped_reports[document_id] = report
    if duplicate_count:
        logger.warning(
            "Bulk upsert payload contained %s duplicate document_ids; keeping last occurrence.",
            duplicate_count,
        )
        validated_reports = list(deduped_reports.values())

    def _dedupe_by_key(
        items: list[dict[str, Any]], key_name: str
    ) -> tuple[list[dict[str, Any]], int]:
        if not items:
            return [], 0
        by_key: dict[str, dict[str, Any]] = {}
        for item in items:
            by_key[item[key_name]] = item
        return list(by_key.values()), len(items) - len(by_key)

    def _dedupe_metadata(items: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], int]:
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
            batch_size=BULK_DB_BATCH_SIZE,
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
            batch_size=BULK_DB_BATCH_SIZE,
        )
        modality_by_code = {
            mod.code: mod
            async for mod in Modality.objects.filter(code__in=modality_codes)
        }

    existing_by_document_id = {
        report.document_id: report
        async for report in Report.objects.filter(document_id__in=document_ids)
    }

    # ── Phase 3: CPU-only build of new_reports / updated_reports lists ──
    now = timezone.now()
    created_ids: list[str] = []
    updated_ids: list[str] = []
    new_reports: list[Report] = []
    updated_reports: list[Report] = []

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

    # ── Phase 4: atomic writes ──
    @sync_to_async(thread_sensitive=True)
    @transaction.atomic
    def _do_atomic_writes() -> None:
        if new_reports:
            Report.objects.bulk_create(new_reports, batch_size=BULK_DB_BATCH_SIZE)
        if updated_reports:
            Report.objects.bulk_update(
                updated_reports,
                fields=[*report_field_names, "language", "updated_at"],
                batch_size=BULK_DB_BATCH_SIZE,
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
                Metadata.objects.bulk_create(metadata_rows, batch_size=BULK_DB_BATCH_SIZE)

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
                modality_through.objects.bulk_create(modality_rows, batch_size=BULK_DB_BATCH_SIZE)

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
                group_through.objects.bulk_create(group_rows, batch_size=BULK_DB_BATCH_SIZE)

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
        data = request.data

        # No `@transaction.atomic` here: `ReportSerializer.create` already
        # opens its own `with transaction.atomic():` block for the multi-step
        # write of Language → Report → groups → Metadata → Modalities.
        # `transaction.on_commit` registered after that block exits fires
        # immediately under no outer transaction (production) or queues until
        # the test wrapper commits (under `django_capture_on_commit_callbacks`).
        @sync_to_async(thread_sensitive=True)
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

        # No `@transaction.atomic` here: `ReportSerializer.create` /
        # `ReportSerializer.update` already open their own
        # `with transaction.atomic():` block for the multi-step writes.
        @sync_to_async(thread_sensitive=True)
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

        @sync_to_async(thread_sensitive=True)
        @transaction.atomic
        def _delete_and_schedule() -> None:
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
