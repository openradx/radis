"""Model field validators for the extractions app."""

import keyword

from django.core.exceptions import ValidationError

OUTPUT_FIELD_NAME_HELP_TEXT = (
    "Letters, digits and underscores only, starting with a letter "
    '(e.g. lung_nodule_size). Names starting with "model_" are reserved.'
)


def validate_output_field_name(name: str) -> None:
    """Ensures a name is safe as an attribute of the generated Pydantic schema.

    Output field names become attributes of a ``pydantic.create_model`` class. Pydantic
    silently drops attributes starting with an underscore (treating them as private) and
    rejects the ``model_`` prefix (its reserved namespace), so neither may be accepted.

    Raises:
        ValidationError: If the name is not a usable identifier.
    """
    if not name.isidentifier() or keyword.iskeyword(name):
        raise ValidationError(
            "Use only letters, digits and underscores, starting with a letter.",
            code="invalid",
        )
    if name.startswith("_"):
        raise ValidationError("Name must not start with an underscore.", code="invalid")
    if name.startswith("model_"):
        raise ValidationError('Names starting with "model_" are reserved.', code="invalid")
