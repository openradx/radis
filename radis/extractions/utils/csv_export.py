"""Helpers for exporting extraction results in CSV format."""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from typing import Any

from django.db.models import QuerySet

from radis.extractions.models import ExtractionInstance, ExtractionJob


def iter_extraction_result_rows(job: ExtractionJob) -> Iterable[Sequence[str]]:
    """Yield rows for the extraction results CSV.

    Args:
        job: The extraction job whose results should be exported.

    Yields:
        Sequences of stringified cell values suitable for csv.writer.
    """

    field_names: list[str] = list(
        job.output_fields.order_by("pk").values_list("name", flat=True)
    )

    header = ["instance_id", "report_id", "is_processed"]
    header.extend(field_names)
    yield header

    instances: QuerySet[ExtractionInstance] = ExtractionInstance.objects.filter(
        task__job=job
    ).order_by("pk")

    for instance in instances.iterator():
        row: list[str] = [
            str(instance.pk),
            str(instance.report_id) if instance.report_id else "",
            "yes" if instance.is_processed else "no",
        ]

        output: dict[str, Any] = instance.output or {}
        for field_name in field_names:
            value = output.get(field_name)
            row.append("" if value is None else str(value))

        yield row
