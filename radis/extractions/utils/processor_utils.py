from typing import Any, Literal

from django.db.models import QuerySet
from pydantic import BaseModel, create_model

from ..models import OutputField, OutputType

type Numeric = float | int


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
            options = tuple(field.selection_options)
            if not options:
                raise ValueError("Selection output requires at least one option.")
            output_type = Literal.__getitem__(options)
        else:
            raise ValueError(f"Unknown data type: {field.output_type}")

        field_definitions[field.name] = (output_type, ...)

    return create_model("OutputFieldsModel", **field_definitions)


def generate_output_fields_prompt(fields: QuerySet[OutputField]) -> str:
    prompt = ""
    for field in fields.all():
        description = field.description
        if OutputType(field.output_type) == OutputType.SELECTION and field.selection_options:
            description = (
                f"{description} (Allowed selections: {', '.join(field.selection_options)})"
            )
        prompt += f"{field.name}: {description}\n"

    return prompt
