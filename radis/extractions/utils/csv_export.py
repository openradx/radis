"""Helpers for exporting extraction results in CSV format."""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from typing import Any

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

    instances = (
        ExtractionInstance.objects.filter(task__job=job)
        .order_by("pk")
        .values_list("pk", "report_id", "is_processed", "output")
    )

    for instance_id, report_id, is_processed, output in instances.iterator():
        row: list[str] = [
            str(instance_id),
            str(report_id) if report_id else "",
            "yes" if is_processed else "no",
        ]

        output_dict: dict[str, Any] = output or {}
        for field_name in field_names:
            value = output_dict.get(field_name)
            row.append("" if value is None else str(value))

        yield row
