from typing import Any

from django.db.models import QuerySet
from pydantic import BaseModel, create_model

from ..models import Question


def generate_questions_schema(questions: QuerySet[Question]) -> type[BaseModel]:
    field_definitions: dict[str, Any] = {}
    for index, _ in enumerate(questions.all()):
        field_definitions[f"question_{index}"] = (bool, ...)

    return create_model("QuestionsModel", field_definitions=field_definitions)


def generate_questions_for_prompt(fields: QuerySet[Question]) -> str:
    prompt = ""
    for index, question in enumerate(fields.all()):
        prompt += f"question_{index}: {question.question}\n"

    return prompt
