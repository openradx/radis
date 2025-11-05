"""Utilities for working with CSV exports."""

from typing import Any

_FORMULA_PREFIXES = ("=", "+", "-", "@")


def sanitize_csv_value(value: Any) -> str:
    """Return a CSV-safe string, preventing spreadsheet formula execution."""
    if value is None:
        return ""
    text = str(value)
    if text and text[0] in _FORMULA_PREFIXES:
        return f"'{text}"
    return text

