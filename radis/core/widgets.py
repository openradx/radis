from __future__ import annotations

from django import forms

# Date inputs should accept both ISO format (for browser widgets) and the user facing
# day/month/year order.
DATE_INPUT_FORMATS = ("%d/%m/%Y", "%Y-%m-%d")
DATE_DISPLAY_FORMAT = "d/m/Y"
DATETIME_DISPLAY_FORMAT = "d/m/Y H:i"


class DatePickerInput(forms.DateInput):
    """HTML5 date input with sensible defaults for RADIS."""

    input_type = "date"

    def __init__(self, attrs: dict[str, str] | None = None, format: str | None = None):
        default_attrs = {"placeholder": "dd/mm/yyyy"}
        if attrs:
            default_attrs.update(attrs)
        super().__init__(attrs=default_attrs, format=format or "%Y-%m-%d")
