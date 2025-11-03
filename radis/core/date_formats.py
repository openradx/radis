"""Shared date and datetime format definitions used across RADIS."""

# User-facing day/month/year ordering for form inputs.
DATE_INPUT_FORMATS = ("%d/%m/%Y", "%Y-%m-%d")

# Template display formats (Django |date filter syntax).
DATE_TEMPLATE_FORMAT = "d/m/Y"
DATETIME_TEMPLATE_FORMAT = "d/m/Y H:i"

# Browser-compliant HTML input format for type=date fields.
DATE_HTML_INPUT_FORMAT = "%Y-%m-%d"
