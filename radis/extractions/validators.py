"""Model field validators for the extractions app."""

from django.core.exceptions import ValidationError

OUTPUT_FIELD_NAME_HELP_TEXT = (
    'Must not start with an underscore or "model_", and must not contain a dot '
    "or a double underscore."
)


def validate_output_field_name(name: str) -> None:
    """Rejects the names that a consumer of the field drops, crashes on or misreads.

    Output field names become attributes of a ``pydantic.create_model`` class, which
    silently drops names starting with an underscore (private attributes) and crashes on
    names that shadow ``BaseModel`` members such as ``model_config``. The results table
    reads each value through a django-tables2 accessor, which splits its path on dots, and
    sorts through the matching Django JSON lookup, which splits on double underscores.
    Anything else, spaces included, works everywhere a name is used.

    Raises:
        ValidationError: If the name is unsafe for one of those consumers.
    """
    if name.startswith("_"):
        raise ValidationError("Name must not start with an underscore.", code="invalid")
    if name.startswith("model_"):
        raise ValidationError('Names starting with "model_" are reserved.', code="invalid")
    if "." in name:
        raise ValidationError("Name must not contain a dot.", code="invalid")
    if "__" in name:
        raise ValidationError("Name must not contain a double underscore.", code="invalid")
