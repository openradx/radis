"""Helpers for exporting subscription inbox items in CSV format."""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from typing import Any

from django.db.models import QuerySet

from radis.subscriptions.models import SubscribedItem, Subscription


def _format_cell(value: Any) -> str:
    """Format a single output value for CSV export."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "yes" if value else "no"
    return str(value)


def iter_subscribed_item_rows(
    subscription: Subscription, queryset: QuerySet[SubscribedItem]
) -> Iterable[Sequence[str]]:
    """Yield rows for the subscription inbox CSV.

    Args:
        subscription: The subscription whose items should be exported.
        queryset: Pre-filtered queryset of SubscribedItems to export.

    Yields:
        Sequences of stringified cell values suitable for csv.writer.
    """
    # Get output field names in PK order (to match dict keys)
    field_names: list[str] = list(
        subscription.output_fields.order_by("pk").values_list("name", flat=True)
    )

    # Header row
    header = [
        "subscribed_item_id",
        "report_id",
        "patient_id",
        "study_date",
        "study_description",
        "modalities",
    ]
    header.extend(field_names)
    yield header

    # Data rows - prefetch related fields for efficiency
    items = queryset.select_related("report").prefetch_related(
        "report__modalities", "subscription__output_fields"
    )

    for item in items.iterator(chunk_size=1000):
        # Format modalities as comma-separated codes
        modality_codes = ",".join(
            item.report.modalities.order_by("code").values_list("code", flat=True)
        )

        # Format study date
        study_date = ""
        if item.report.study_datetime:
            study_date = item.report.study_datetime.strftime("%Y-%m-%d")

        row = [
            str(item.pk),
            str(item.report.pk),
            item.report.patient_id or "",
            study_date,
            item.report.study_description or "",
            modality_codes,
        ]

        # Add extraction results (keyed by field PK as string)
        extraction_results: dict[str, Any] = item.extraction_results or {}
        for field_pk in subscription.output_fields.order_by("pk").values_list("pk", flat=True):
            value = extraction_results.get(str(field_pk))
            row.append(_format_cell(value))

        yield row
