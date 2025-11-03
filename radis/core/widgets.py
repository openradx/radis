from __future__ import annotations

from django import forms

from .date_formats import DATE_HTML_INPUT_FORMAT


class DatePickerInput(forms.DateInput):
    """HTML5 date input with sensible defaults for RADIS."""

    input_type = "date"

    def __init__(self, attrs: dict[str, str] | None = None, format: str | None = None):
        super().__init__(attrs=attrs, format=format or DATE_HTML_INPUT_FORMAT)
