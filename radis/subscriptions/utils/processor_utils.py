from __future__ import annotations

from typing import Any

from django.db.models import QuerySet
from pydantic import BaseModel, create_model

from radis.extractions.models import OutputField

from ..models import FilterQuestion


def _filter_question_field_name(question: FilterQuestion) -> str:
    return f"question_{question.pk}"


def get_filter_question_field_name(question: FilterQuestion) -> str:
    return _filter_question_field_name(question)


def get_output_field_name(field: OutputField) -> str:
    return field.name


def generate_filter_questions_schema(questions: QuerySet[FilterQuestion]) -> type[BaseModel]:
    field_definitions: dict[str, Any] = {}

    for question in questions.order_by("pk").all():
        field_name = _filter_question_field_name(question)
        field_definitions[field_name] = (bool, ...)

    model_name = "SubscriptionFilterResultsModel"
    return create_model(model_name, **field_definitions)


def generate_filter_questions_prompt(questions: QuerySet[FilterQuestion]) -> str:
    prompt = ""
    for question in questions.order_by("pk").all():
        prompt += f"{_filter_question_field_name(question)}: {question.question}\n"
    return prompt
