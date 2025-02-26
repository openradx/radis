from typing import Any

from django.db.models import QuerySet
from pydantic import BaseModel, create_model

from ..models import OutputField, OutputType

type Numeric = float | int


def generate_output_fields_schema(fields: QuerySet[OutputField]) -> type[BaseModel]:
    field_definitions: dict[str, Any] = {}
    for field in fields.all():
        if field.output_type == OutputType.TEXT:
            output_type = str
        elif field.output_type == OutputType.NUMERIC:
            output_type = Numeric
        elif field.output_type == OutputType.BOOLEAN:
            output_type = bool
        else:
            raise ValueError(f"Unknown data type: {field.output_type}")

        field_definitions[field.name] = (output_type, ...)

    return create_model("OutputFieldsModel", **field_definitions)


def generate_output_fields_prompt(fields: QuerySet[OutputField]) -> str:
    prompt = ""
    for field in fields.all():
        prompt += f"{field.name}: {field.description}\n"

    return prompt
