from collections.abc import Iterable
from typing import Any, Literal

from pydantic import BaseModel, create_model

from ..models import OutputField, OutputType

type Numeric = float | int


def generate_output_fields_schema(
    fields: Iterable[OutputField], *, nullable: bool = False
) -> type[BaseModel]:
    """Build a Pydantic model that describes the structure the extractor must output.

    With ``nullable=True`` every value may also be ``null`` (each key itself
    stays required) — for prompts that instruct the model to answer null when
    the report does not contain the requested information. Without it such a
    schema could never validate; a Selection ``Literal`` in particular would
    force the model to fabricate one of the options.
    """
    field_definitions: dict[str, Any] = {}
    for field in fields:
        output_type: Any
        if field.output_type == OutputType.TEXT:
            output_type = str
        elif field.output_type == OutputType.NUMERIC:
            output_type = Numeric
        elif field.output_type == OutputType.BOOLEAN:
            output_type = bool
        elif field.output_type == OutputType.SELECTION:
            options = tuple(field.selection_options)
            if not options:
                raise ValueError("Selection output requires at least one option.")
            output_type = Literal[*options]
        else:
            raise ValueError(f"Unknown data type: {field.output_type}")

        if field.is_array:
            # If the field stores multiple values, use a list[...] of the base type above.
            output_type = list[output_type]

        if nullable:
            output_type = output_type | None

        field_definitions[field.name] = (output_type, ...)

    return create_model("OutputFieldsModel", **field_definitions)


def generate_output_fields_prompt(fields: Iterable[OutputField]) -> str:
    """Build a human-readable prompt describing output fields for the extractor."""
    prompt = ""
    for field in fields:
        description = field.description
        if OutputType(field.output_type) == OutputType.SELECTION and field.selection_options:
            description = (
                f"{description} (Allowed selections: {', '.join(field.selection_options)})"
            )
        if field.is_array:
            description = (
                f"{description} (Return an array of "
                f"{field.get_output_type_display().lower()} values.)"
            )
        prompt += f"{field.name}: {description}\n"

    return prompt
