"""Shared validation utilities for the extractions app."""

from django.conf import settings
from django.core.exceptions import ValidationError


def validate_selection_options(options: list) -> list[str]:
    """
    Validates selection options for output fields.

    Args:
        options: A list of selection options to validate

    Returns:
        A list of cleaned (stripped) selection option strings

    Raises:
        ValidationError: If validation fails for any of these reasons:
            - Options is not a list
            - Any option is not a string
            - Any option is empty after stripping
            - Too many options (exceeds settings.EXTRACTION_MAX_SELECTION_OPTIONS)
            - Options are not unique
    """
    if not isinstance(options, list):
        raise ValidationError("Selection options must be a list.")

    cleaned_options = []
    for option in options:
        if not isinstance(option, str):
            raise ValidationError("All selection options must be text.")

        stripped = option.strip()
        if not stripped:
            raise ValidationError("Selection options cannot be empty strings.")

        cleaned_options.append(stripped)

    max_options = settings.EXTRACTION_MAX_SELECTION_OPTIONS
    if len(cleaned_options) > max_options:
        raise ValidationError(f"Provide at most {max_options} selection options.")

    if len(set(cleaned_options)) != len(cleaned_options):
        raise ValidationError("Selection options must be unique.")

    return cleaned_options
