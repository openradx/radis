"""Async domain operations for the report API.

Pure async write operations using native async ORM. None of these
functions own a transaction; atomicity is the caller's responsibility.
"""
import logging
from typing import Any

from ..models import Language, Metadata, Modality, Report

logger = logging.getLogger(__name__)


async def create_report_from_validated(
    validated_data: dict[str, Any],
) -> Report:
    """Create a Report and its nested associations from validated payload.

    Pops `language`, `groups`, `metadata`, `modalities` out of
    `validated_data` and uses the remaining keys as direct Report fields.
    """
    language = validated_data.pop("language")
    groups = validated_data.pop("groups")
    metadata = validated_data.pop("metadata")
    modalities = validated_data.pop("modalities")

    language_instance, _ = await Language.objects.aget_or_create(**language)
    report = await Report.objects.acreate(
        **validated_data, language=language_instance
    )

    await report.groups.aset(groups)

    for item in metadata:
        await Metadata.objects.acreate(report=report, **item)

    modality_instances: list[Modality] = []
    for modality in modalities:
        instance, _ = await Modality.objects.aget_or_create(**modality)
        modality_instances.append(instance)
    await report.modalities.aset(modality_instances)

    return report


async def update_report_from_validated(
    report: Report, validated_data: dict[str, Any]
) -> Report:
    """Replace all mutable fields and nested associations on an existing Report.

    Matches the legacy `ReportSerializer.update` semantics: metadata is
    fully replaced (delete + recreate), modalities and groups are reset
    to the provided sets.
    """
    language = validated_data.pop("language")
    groups = validated_data.pop("groups")
    metadata = validated_data.pop("metadata")
    modalities = validated_data.pop("modalities")

    language_instance = await Language.objects.aget(**language)
    report.language = language_instance
    for attr, value in validated_data.items():
        setattr(report, attr, value)
    await report.asave()

    await report.groups.aset(groups)

    await report.metadata.all().adelete()
    for item in metadata:
        await Metadata.objects.acreate(report=report, **item)

    await report.modalities.aclear()
    modality_instances: list[Modality] = []
    for modality in modalities:
        instance, _ = await Modality.objects.aget_or_create(**modality)
        modality_instances.append(instance)
    await report.modalities.aset(modality_instances)

    return report


async def delete_report(report: Report) -> None:
    """Delete a single Report row."""
    await report.adelete()
