"""Helpers for exporting subscription inbox items in CSV format."""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from typing import Any

from django.db.models import QuerySet

from radis.core.utils.csv_export import escape_formula, format_cell
from radis.subscriptions.models import SubscribedItem, Subscription


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
    # Materialize names and PKs once, in the same PK order, so the columns and
    # the pk-keyed extraction_results lookups stay aligned per row.
    field_names: list[str] = list(
        subscription.output_fields.order_by("pk").values_list("name", flat=True)
    )
    field_pks: list[int] = list(
        subscription.output_fields.order_by("pk").values_list("pk", flat=True)
    )

    header = [
        "subscribed_item_id",
        "report_id",
        "patient_id",
        "study_date",
        "study_description",
        "modalities",
    ]
    header.extend(escape_formula(name) for name in field_names)
    yield header

    items = queryset.select_related("report").prefetch_related("report__modalities")

    for item in items.iterator(chunk_size=1000):
        modality_codes = ",".join(
            modality.code
            for modality in sorted(
                item.report.modalities.all(),
                key=lambda modality: modality.code,
            )
        )

        study_date = ""
        if item.report.study_datetime:
            study_date = item.report.study_datetime.strftime("%Y-%m-%d")

        row = [
            str(item.pk),
            str(item.report.pk),
            escape_formula(item.report.patient_id or ""),
            study_date,
            escape_formula(item.report.study_description or ""),
            modality_codes,
        ]

        extraction_results: dict[str, Any] = item.extraction_results or {}
        for field_pk in field_pks:
            row.append(format_cell(extraction_results.get(str(field_pk))))

        yield row
