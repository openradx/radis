"""Model field validators for the extractions app."""

import keyword

from django.core.exceptions import ValidationError

OUTPUT_FIELD_NAME_HELP_TEXT = (
    "Letters, digits and single underscores, starting with a letter "
    '(e.g. lung_nodule_size). Names starting with "model_" are reserved.'
)


def validate_output_field_name(name: str) -> None:
    """Ensures a name is safe as a Pydantic schema attribute and a table accessor.

    Output field names become attributes of a ``pydantic.create_model`` class. Pydantic
    silently drops attributes starting with an underscore (treating them as private) and
    rejects names in its reserved ``model_`` namespace. The results table reaches each
    value through a django-tables2 accessor, which splits its path on ``__``.

    Raises:
        ValidationError: If the name is not a usable identifier.
    """
    if not name.isidentifier():
        raise ValidationError(
            "Use only letters, digits and underscores, starting with a letter.",
            code="invalid",
        )
    if keyword.iskeyword(name):
        raise ValidationError(
            '"%(name)s" is a reserved word.', code="invalid", params={"name": name}
        )
    if name.startswith("_"):
        raise ValidationError("Name must not start with an underscore.", code="invalid")
    if "__" in name:
        raise ValidationError("Name must not contain two underscores in a row.", code="invalid")
    if name.startswith("model_"):
        raise ValidationError('Names starting with "model_" are reserved.', code="invalid")
