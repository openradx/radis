"""Shared helpers for streaming CSV downloads."""

from __future__ import annotations

import csv
import logging
from collections.abc import Iterable, Iterator, Sequence
from typing import Any

from django.http import StreamingHttpResponse

logger = logging.getLogger(__name__)


class _Echo:
    """Pseudo-buffer whose write() returns the value, for csv.writer streaming."""

    def write(self, value: str) -> str:
        return value


def escape_formula(text: str) -> str:
    """Neutralize spreadsheet formula injection (CSV injection).

    Exported content may be attacker-influenceable (report bodies, LLM
    output, user-defined field names); a leading ``=``/``+``/``-``/``@``/
    tab/CR makes Excel/LibreOffice interpret the cell as a formula, so such
    cells are prefixed with a single quote.
    """
    if text and text[0] in ("=", "+", "-", "@", "\t", "\r"):
        return "'" + text
    return text


def format_cell(value: Any) -> str:
    """Format a single output value for CSV export."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "yes" if value else "no"
    return escape_formula(str(value))


def _stream(rows: Iterable[Sequence[str]], error_context: str) -> Iterator[str]:
    pseudo_buffer = _Echo()
    writer = csv.writer(pseudo_buffer)
    yield "\ufeff"  # UTF-8 BOM for Excel compatibility
    try:
        for row in rows:
            yield writer.writerow(row)
    except Exception:
        # The response is already streaming, so an error page is impossible;
        # emit a marker row so a truncated download is distinguishable from a
        # complete one.
        logger.exception("CSV export aborted mid-stream (%s)", error_context)
        yield writer.writerow(["ERROR", "export incomplete due to a server error"])


def stream_csv_response(
    rows: Iterable[Sequence[str]], filename: str, error_context: str
) -> StreamingHttpResponse:
    """Build a streaming text/csv attachment response for the given rows.

    Args:
        rows: Iterable of CSV rows (sequences of already-formatted strings).
        filename: Download filename for the Content-Disposition header.
        error_context: Short description used in the log if the stream aborts.
    """
    response = StreamingHttpResponse(_stream(rows, error_context), content_type="text/csv")
    response["Content-Disposition"] = f'attachment; filename="{filename}"'
    return response
