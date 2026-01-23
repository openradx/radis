import logging
from typing import Any

from django.db import transaction
from django.http import Http404
from django.utils import timezone
from rest_framework import mixins, status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import MethodNotAllowed, ValidationError
from rest_framework.permissions import IsAdminUser
from rest_framework.request import Request, clone_request
from rest_framework.response import Response
from rest_framework.serializers import BaseSerializer

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


def _bulk_upsert_reports(validated_reports: list[dict[str, Any]]) -> tuple[list[str], list[str]]:
    if not validated_reports:
        return [], []

    document_ids = [report["document_id"] for report in validated_reports]

    language_codes = {report["language"]["code"] for report in validated_reports}
    language_by_code = {lang.code: lang for lang in Language.objects.filter(code__in=language_codes)}
    missing_language_codes = language_codes - language_by_code.keys()
    if missing_language_codes:
        Language.objects.bulk_create(
            [Language(code=code) for code in missing_language_codes],
            ignore_conflicts=True,
            batch_size=BULK_DB_BATCH_SIZE,
        )
        language_by_code = {
            lang.code: lang for lang in Language.objects.filter(code__in=language_codes)
        }

    modality_codes = {
        modality["code"]
        for report in validated_reports
        for modality in report.get("modalities", [])
    }
    modality_by_code = {mod.code: mod for mod in Modality.objects.filter(code__in=modality_codes)}
    missing_modality_codes = modality_codes - modality_by_code.keys()
    if missing_modality_codes:
        Modality.objects.bulk_create(
            [Modality(code=code) for code in missing_modality_codes],
            ignore_conflicts=True,
            batch_size=BULK_DB_BATCH_SIZE,
        )
        modality_by_code = {
            mod.code: mod for mod in Modality.objects.filter(code__in=modality_codes)
        }

    existing_reports = Report.objects.filter(document_id__in=document_ids)
    existing_by_document_id = {report.document_id: report for report in existing_reports}

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
            existing.language_id = language.id
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

    with transaction.atomic():
        if new_reports:
            Report.objects.bulk_create(new_reports, batch_size=BULK_DB_BATCH_SIZE)

        if updated_reports:
            Report.objects.bulk_update(
                updated_reports,
                fields=[*report_field_names, "language", "updated_at"],
                batch_size=BULK_DB_BATCH_SIZE,
            )

        report_id_by_document_id = {
            report.document_id: report.id
            for report in Report.objects.filter(document_id__in=document_ids).only("id", "document_id")
        }
        report_ids = list(report_id_by_document_id.values())

        if report_ids:
            Metadata.objects.filter(report_id__in=report_ids).delete()

            metadata_rows: list[Metadata] = []
            for report_data in validated_reports:
                report_id = report_id_by_document_id[report_data["document_id"]]
                for item in report_data.get("metadata", []):
                    metadata_rows.append(
                        Metadata(report_id=report_id, key=item["key"], value=item["value"])
                    )
            if metadata_rows:
                Metadata.objects.bulk_create(metadata_rows, batch_size=BULK_DB_BATCH_SIZE)

            modality_through = Report.modalities.through
            modality_through.objects.filter(report_id__in=report_ids).delete()

            modality_rows = []
            for report_data in validated_reports:
                report_id = report_id_by_document_id[report_data["document_id"]]
                for modality in report_data.get("modalities", []):
                    modality_id = modality_by_code[modality["code"]].id
                    modality_rows.append(
                        modality_through(report_id=report_id, modality_id=modality_id)
                    )
            if modality_rows:
                modality_through.objects.bulk_create(modality_rows, batch_size=BULK_DB_BATCH_SIZE)

            group_through = Report.groups.through
            group_through.objects.filter(report_id__in=report_ids).delete()

            group_rows = []
            for report_data in validated_reports:
                report_id = report_id_by_document_id[report_data["document_id"]]
                for group in report_data.get("groups", []):
                    group_rows.append(group_through(report_id=report_id, group_id=group.id))
            if group_rows:
                group_through.objects.bulk_create(group_rows, batch_size=BULK_DB_BATCH_SIZE)

        def on_commit():
            if created_ids:
                created_reports = list(Report.objects.filter(document_id__in=created_ids))
                for handler in reports_created_handlers:
                    handler.handle(created_reports)
            if updated_ids:
                updated_reports = list(Report.objects.filter(document_id__in=updated_ids))
                for handler in reports_updated_handlers:
                    handler.handle(updated_reports)

        transaction.on_commit(on_commit)

    return created_ids, updated_ids


class ReportViewSet(
    mixins.CreateModelMixin,
    mixins.DestroyModelMixin,
    mixins.RetrieveModelMixin,
    mixins.UpdateModelMixin,
    viewsets.GenericViewSet,
):
    """ViewSet for fetch, creating, updating, and deleting Reports.

    Only admins (staff users) can do that.
    """

    serializer_class = ReportSerializer
    queryset = Report.objects.all()
    lookup_field = "document_id"
    permission_classes = [IsAdminUser]

    def get_serializer(self, *args: Any, **kwargs: Any) -> BaseSerializer:
        if isinstance(kwargs.get("data", {}), list):
            kwargs["many"] = True
        return super().get_serializer(*args, **kwargs)

    def retrieve(self, request: Request, *args: Any, **kwargs: Any) -> Response:
        """Retrieve a single Report.

        It also fetches the associated documents from all external databases.
        """
        full = request.GET.get("full", "").lower() in ["true", "1", "yes"]

        instance: Report = self.get_object()
        serializer = self.get_serializer(instance)
        data = serializer.data

        if full:
            documents = {}
            for fetcher in document_fetchers.values():
                document = fetcher.fetch(instance)
                if document:
                    documents[fetcher.source] = document
            data["documents"] = documents

        return Response(data)

    def perform_create(self, serializer: BaseSerializer) -> None:
        super().perform_create(serializer)
        assert serializer.instance
        reports: list[Report] | Report = serializer.instance
        if not isinstance(reports, list):
            reports = [reports]

        def on_commit():
            for handler in reports_created_handlers:
                document_ids = [report.document_id for report in reports]
                logger.debug(f"{handler.name} - handle newly created reports: {document_ids}")
                handler.handle(reports)

        transaction.on_commit(on_commit)

    def update(self, request: Request, *args: Any, **kwargs: Any) -> Response:
        # DRF itself does not support upsert.
        # Workaround adapted from https://gist.github.com/tomchristie/a2ace4577eff2c603b1b
        upsert = request.GET.get("upsert", "").lower() in ["true", "1", "yes"]
        if not upsert:
            return super().update(request, *args, **kwargs)
        else:
            instance = self.get_object_or_none()
            serializer = self.get_serializer(instance, data=request.data)
            serializer.is_valid(raise_exception=True)

            if instance is None:
                self.perform_create(serializer)
                return Response(serializer.data, status=status.HTTP_201_CREATED)

            self.perform_update(serializer)
            return Response(serializer.data)

    @action(detail=False, methods=["post"], url_path="bulk-upsert")
    def bulk_upsert(self, request: Request) -> Response:
        if not isinstance(request.data, list):
            return Response(
                {"detail": "Expected a list of report objects."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        valid_payloads: list[dict[str, Any]] = []
        errors: list[dict[str, Any]] = []
        for index, payload in enumerate(request.data):
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
            created_ids, updated_ids = _bulk_upsert_reports(valid_payloads)

        response_body: dict[str, Any] = {
            "created": len(created_ids),
            "updated": len(updated_ids),
            "invalid": len(errors),
        }
        if errors:
            max_errors = 50
            response_body["errors"] = errors[:max_errors]
            response_body["errors_truncated"] = len(errors) > max_errors
        return Response(response_body)

    def get_object_or_none(self) -> Report | None:
        try:
            return self.get_object()
        except Http404:
            if self.request.method == "PUT":
                self.check_permissions(clone_request(self.request, "POST"))
            else:
                raise

    def perform_update(self, serializer: BaseSerializer) -> None:
        super().perform_update(serializer)
        assert serializer.instance
        reports: list[Report] | Report = serializer.instance
        if not isinstance(reports, list):
            reports = [reports]

        def on_commit():
            for handler in reports_updated_handlers:
                document_ids = [report.document_id for report in reports]
                logger.debug(f"{handler.name} - handle updated reports: {document_ids}")
                handler.handle(reports)

        transaction.on_commit(on_commit)

    def partial_update(self, request: Request, *args: Any, **kwargs: Any) -> Response:
        # Disallow partial updates
        assert request.method
        raise MethodNotAllowed(request.method)

    def perform_destroy(self, instance: Report) -> None:
        super().perform_destroy(instance)

        def on_commit():
            for handler in reports_deleted_handlers:
                logger.debug(f"{handler.name} - handle deleted report: {instance.document_id}")
                handler.handle([instance])

        transaction.on_commit(on_commit)
