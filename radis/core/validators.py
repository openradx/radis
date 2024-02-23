from typing import Any

from django.core.exceptions import ValidationError
from django.core.validators import RegexValidator

no_backslash_char_validator = RegexValidator(
    regex=r"\\",
    message="Contains invalid backslash character",
    inverse_match=True,
)


no_control_chars_validator = RegexValidator(
    regex=r"[\f\n\r]",
    message="Contains invalid control characters.",
    inverse_match=True,
)

no_wildcard_chars_validator = RegexValidator(
    regex=r"[\*\?]",
    message="Contains invalid wildcard characters.",
    inverse_match=True,
)


def validate_patient_sex(patient_sex: str):
    if patient_sex not in ["M", "F", "O"]:
        raise ValidationError(f"Invalid patient sex: {patient_sex}")


def validate_metadata(metadata: Any):
    if not isinstance(metadata, dict):
        raise ValidationError("Invalid metadata: not a dictionary")
    for key, value in metadata.items():
        if not isinstance(value, str):
            raise ValidationError(f"Invalid metadata: '{key}' is not a string")
