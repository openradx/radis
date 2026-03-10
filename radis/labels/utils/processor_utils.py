from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, create_model

from ..models import LabelQuestion


def _generate_label_answer_schema(index: int, question: LabelQuestion) -> type[BaseModel]:
    """Create a strict answer schema for one question.

    We enforce that `choice` is exactly one of the configured choice values using `Literal[...]`.
    This mirrors how the `selectionOutputType` branch enforces allowed options for extraction
    fields.
    """

    choice_values = tuple(
        choice.value
        for choice in question.choices.all()
        if isinstance(choice.value, str) and choice.value
    )
    if not choice_values:
        raise ValueError(
            f"LabelQuestion {question.pk or '<unsaved>'} has no valid choices configured."
        )

    ChoiceType = Literal[*choice_values]
    return create_model(
        f"LabelAnswer_{index}",
        choice=(ChoiceType, ...),
        confidence=(float | None, None),
        rationale=(str | None, None),
    )


def generate_labeling_schema(questions: list[LabelQuestion]) -> type[BaseModel]:
    field_definitions: dict[str, Any] = {}
    for index, question in enumerate(questions):
        AnswerSchema = _generate_label_answer_schema(index, question)
        field_definitions[f"question_{index}"] = (AnswerSchema, ...)

    return create_model("LabelingModel", **field_definitions)


def generate_questions_for_prompt(questions: list[LabelQuestion]) -> str:
    prompt = ""
    for index, question in enumerate(questions):
        choices = ", ".join(
            [f"{choice.value} ({choice.label})" for choice in question.choices.all()]
        )
        prompt += f"question_{index}: {question.question}\n"
        prompt += f"choices (return exactly one choice value): {choices}\n"

    return prompt
