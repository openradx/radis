"""Helpers for exporting subscription inbox items in CSV format."""

from __future__ import annotations

import logging
from collections.abc import Iterable, Sequence
from typing import Any

from django.db.models import QuerySet

from radis.extractions.utils.csv_export import _escape_formula
from radis.subscriptions.models import SubscribedItem, Subscription

logger = logging.getLogger(__name__)


def _format_cell(value: Any) -> str:
    """Format a single output value for CSV export."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "yes" if value else "no"
    return _escape_formula(str(value))


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

    # Header row
    header = [
        "subscribed_item_id",
        "report_id",
        "patient_id",
        "study_date",
        "study_description",
        "modalities",
    ]
    header.extend(_escape_formula(name) for name in field_names)
    yield header

    items = queryset.select_related("report").prefetch_related("report__modalities")

    try:
        for item in items.iterator(chunk_size=1000):
            # Format modalities as comma-separated codes
            modality_codes = ",".join(
                modality.code
                for modality in sorted(
                    item.report.modalities.all(),
                    key=lambda modality: modality.code,
                )
            )

            # Format study date
            study_date = ""
            if item.report.study_datetime:
                study_date = item.report.study_datetime.strftime("%Y-%m-%d")

            row = [
                str(item.pk),
                str(item.report.pk),
                _escape_formula(item.report.patient_id or ""),
                study_date,
                _escape_formula(item.report.study_description or ""),
                modality_codes,
            ]

            # Add extraction results (keyed by field PK as string)
            extraction_results: dict[str, Any] = item.extraction_results or {}
            for field_pk in field_pks:
                value = extraction_results.get(str(field_pk))
                row.append(_format_cell(value))

            yield row
    except Exception:
        # The response is already streaming, so an error page is impossible;
        # emit a marker row so a truncated download is distinguishable from a
        # complete one.
        logger.exception(
            "Subscription CSV export aborted mid-stream for subscription %s", subscription.pk
        )
        yield ["ERROR", "export incomplete due to a server error", "", "", "", ""]
