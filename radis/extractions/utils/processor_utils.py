from typing import Any, Literal

from django.db.models import QuerySet
from pydantic import BaseModel, create_model

from ..models import OutputField, OutputType

type Numeric = float | int

"""Build a Pydantic model that describes the structure the extractor must output"""


def generate_output_fields_schema(fields: QuerySet[OutputField]) -> type[BaseModel]:
    field_definitions: dict[str, Any] = {}
    for field in fields.all():
        field_type = OutputType(field.output_type)
        if field_type == OutputType.TEXT:
            output_type = str
        elif field_type == OutputType.NUMERIC:
            output_type = Numeric
        elif field_type == OutputType.BOOLEAN:
            output_type = bool
        elif field_type == OutputType.SELECTION:
            # Grab the saved selection_options list and convert it to a tuple.
            options = tuple(field.selection_options)
            if not options:
                raise ValueError("Selection output requires at least one option.")
            # Set the output type to a Literal of the options so that only these values
            # can be accepted.
            output_type = Literal[*options]
        else:
            raise ValueError(f"Unknown data type: {field.output_type}")

        if field.is_array:
            # If the field stores multiple values, use a list[...] of the base type above.
            output_type = list[output_type]

        field_definitions[field.name] = (output_type, ...)

    return create_model("OutputFieldsModel", **field_definitions)


def generate_output_fields_prompt(fields: QuerySet[OutputField]) -> str:
    # Build a human-readable prompt that mirrors the same selection/array rules.
    prompt = ""
    for field in fields.all():
        description = field.description
        if OutputType(field.output_type) == OutputType.SELECTION and field.selection_options:
            # Tell the processor exactly which selections it may return.
            description = (
                f"{description} (Allowed selections: {', '.join(field.selection_options)})"
            )
        if field.is_array:
            # Remind the processor to return an array, not a single value.
            description = (
                f"{description} (Return an array of "
                f"{field.get_output_type_display().lower()} values.)"
            )
        prompt += f"{field.name}: {description}\n"

    return prompt
