from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from django.db.models import QuerySet
from pydantic import BaseModel, create_model

from radis.extractions.models import OutputField, OutputType

from ..models import FilterQuestion

type Numeric = float | int


@dataclass(slots=True)
class FilterSchemaBundle:
    schema: type[BaseModel]
    mapping: list[tuple[str, FilterQuestion]]


@dataclass(slots=True)
class ExtractionSchemaBundle:
    schema: type[BaseModel]
    mapping: list[tuple[str, OutputField]]


def build_filter_schema(questions: QuerySet[FilterQuestion]) -> FilterSchemaBundle:
    field_definitions: dict[str, Any] = {}
    mapping: list[tuple[str, FilterQuestion]] = []

    for index, question in enumerate(questions.all()):
        field_name = f"filter_{index}"
        field_definitions[field_name] = (bool, ...)
        mapping.append((field_name, question))

    model_name = "SubscriptionFilterResultsModel"
    schema = (
        create_model(model_name, **field_definitions)
        if field_definitions
        else create_model(model_name)
    )
    return FilterSchemaBundle(schema, mapping)


def build_extraction_schema(fields: QuerySet[OutputField]) -> ExtractionSchemaBundle:
    field_definitions: dict[str, Any] = {}
    mapping: list[tuple[str, OutputField]] = []

    for index, field in enumerate(fields.all()):
        field_name = f"extraction_{index}"
        if field.output_type == OutputType.TEXT:
            output_type = str
        elif field.output_type == OutputType.NUMERIC:
            output_type = Numeric
        elif field.output_type == OutputType.BOOLEAN:
            output_type = bool
        else:
            raise ValueError(f"Unknown output type: {field.output_type}")

        field_definitions[field_name] = (output_type, ...)

        mapping.append((field_name, field))

    model_name = "SubscriptionExtractionResultsModel"
    schema = (
        create_model(model_name, **field_definitions)
        if field_definitions
        else create_model(model_name)
    )
    return ExtractionSchemaBundle(schema, mapping)


def generate_filter_questions_prompt(mapping: list[tuple[str, FilterQuestion]]) -> str:
    if not mapping:
        return "None"

    lines: list[str] = []
    for field_name, question in mapping:
        lines.append(f"{field_name}: {question.question}")
    return "\n".join(lines)


def generate_output_fields_prompt(mapping: list[tuple[str, OutputField]]) -> str:
    if not mapping:
        return "None"

    lines: list[str] = []
    for field_name, field in mapping:
        description = field.description or "No description provided."
        lines.append(
            f"{field_name}: {field.name} — {description} [type: {field.get_output_type_display()}]"
        )
    return "\n".join(lines)
