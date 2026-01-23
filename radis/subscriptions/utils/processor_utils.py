from __future__ import annotations

from typing import Any, Iterable

from pydantic import BaseModel, create_model

from radis.extractions.models import OutputField

from ..models import FilterQuestion


def get_filter_question_field_name(question: FilterQuestion) -> str:
    return f"question_{question.pk}"


def get_output_field_name(field: OutputField) -> str:
    return field.name


def generate_filter_questions_schema(questions: Iterable[FilterQuestion]) -> type[BaseModel]:
    field_definitions: dict[str, Any] = {}

    for question in questions:
        field_name = get_filter_question_field_name(question)
        field_definitions[field_name] = (bool, ...)

    model_name = "SubscriptionFilterResultsModel"
    return create_model(model_name, **field_definitions)


def generate_filter_questions_prompt(questions: Iterable[FilterQuestion]) -> str:
    prompt = ""
    for question in questions:
        prompt += f"{get_filter_question_field_name(question)}: {question.question}\n"
    return prompt
