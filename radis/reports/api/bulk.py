# radis/reports/api/bulk.py
import logging
from typing import Any

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from radis.pgsearch.tasks import enqueue_bulk_index_reports
from radis.pgsearch.utils.indexing import bulk_upsert_report_search_vectors

from ..models import Language, Metadata, Modality, Report
from ..site import reports_created_handlers, reports_updated_handlers

logger = logging.getLogger(__name__)

BULK_DB_BATCH_SIZE = 1000


def bulk_upsert_reports(
    validated_reports: list[dict[str, Any]],
) -> tuple[list[str], list[str]]:
    if not validated_reports:
        return [], []

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
            key = item[key_name]
            by_key[key] = item
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

    language_codes = {report["language"]["code"] for report in validated_reports}
    language_by_code = {
        lang.code: lang for lang in Language.objects.filter(code__in=language_codes)
    }
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
                updated_reports = list(Report.objects.filter(document_id__in=updated_ids))
                for handler in reports_updated_handlers:
                    handler.handle(updated_reports)
            if touched_report_ids:
                if settings.PGSEARCH_SYNC_INDEXING:
                    bulk_upsert_report_search_vectors(touched_report_ids)
                else:
                    enqueue_bulk_index_reports(touched_report_ids)

        transaction.on_commit(on_commit)

    return created_ids, updated_ids
