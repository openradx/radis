"""Helpers for exporting extraction results in CSV format."""

from __future__ import annotations

import logging
from collections.abc import Iterable, Sequence
from typing import Any

from radis.extractions.models import ExtractionInstance, ExtractionJob

logger = logging.getLogger(__name__)


def _escape_formula(text: str) -> str:
    """Neutralize spreadsheet formula injection (CSV injection).

    Report content and LLM output are attacker-influenceable; a leading
    ``=``/``+``/``-``/``@``/tab/CR makes Excel/LibreOffice interpret the cell
    as a formula, so such cells are prefixed with a single quote.
    """
    if text and text[0] in ("=", "+", "-", "@", "\t", "\r"):
        return "'" + text
    return text


def _format_cell(value: Any) -> str:
    """Format a single output value for CSV export."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "yes" if value else "no"
    return _escape_formula(str(value))


def iter_extraction_result_rows(job: ExtractionJob) -> Iterable[Sequence[str]]:
    """Yield rows for the extraction results CSV.

    Args:
        job: The extraction job whose results should be exported.

    Yields:
        Sequences of stringified cell values suitable for csv.writer.
    """

    field_names: list[str] = list(job.output_fields.order_by("pk").values_list("name", flat=True))

    header = ["instance_id", "report_id", "is_processed"]
    header.extend(_escape_formula(name) for name in field_names)
    yield header

    instances = (
        ExtractionInstance.objects.filter(task__job=job)
        .order_by("pk")
        .values_list("pk", "report_id", "is_processed", "output")
    )

    try:
        for instance_id, report_id, is_processed, output in instances.iterator():
            row: list[str] = [
                str(instance_id),
                str(report_id) if report_id else "",
                "yes" if is_processed else "no",
            ]

            output_dict: dict[str, Any] = output or {}
            for field_name in field_names:
                value = output_dict.get(field_name)
                row.append(_format_cell(value))

            yield row
    except Exception:
        # The response is already streaming, so an error page is impossible;
        # emit a marker row so a truncated download is distinguishable from a
        # complete one.
        logger.exception("Extraction CSV export aborted mid-stream for job %s", job.pk)
        yield ["ERROR", "export incomplete due to a server error", ""]
