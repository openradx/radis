from typing import Any

from django.db.models import QuerySet
from pydantic import BaseModel, create_model

from ..models import QuestionField


def generate_question_fields_schema(fields: QuerySet[QuestionField]) -> type[BaseModel]:
    field_definitions: dict[str, Any] = {}
    for field in fields.all():
        field_definitions[field.name] = (bool, ...)

    return create_model("QuestionFieldsModel", field_definitions=field_definitions)


def generate_question_fields_prompt(fields: QuerySet[QuestionField]) -> str:
    prompt = ""
    for field in fields.all():
        prompt += f"{field.name}: {field.question}\n"

    return prompt
