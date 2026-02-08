from __future__ import annotations

from typing import Any

from pydantic import BaseModel, create_model

from ..models import LabelQuestion


class LabelAnswer(BaseModel):
    choice: str
    confidence: float | None = None
    rationale: str | None = None


def generate_labeling_schema(questions: list[LabelQuestion]) -> type[BaseModel]:
    field_definitions: dict[str, Any] = {}
    for index, _ in enumerate(questions):
        field_definitions[f"question_{index}"] = (LabelAnswer, ...)

    return create_model("LabelingModel", **field_definitions)


def generate_questions_for_prompt(questions: list[LabelQuestion]) -> str:
    prompt = ""
    for index, question in enumerate(questions):
        choices = ", ".join(
            [f"{choice.value} ({choice.label})" for choice in question.choices.all()]
        )
        prompt += f"question_{index}: {question.question}\n"
        prompt += f"choices: {choices}\n"

    return prompt
