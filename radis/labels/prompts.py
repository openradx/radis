import re
from string import Template
from typing import Literal

from django.conf import settings
from pydantic import BaseModel, ConfigDict, create_model

from .models import Question


def _format_question_lines(questions: list[Question]) -> str:
    return "\n".join(f"- {q.label}: {q.text}" for q in questions)


def render_questions_prompt(report_body: str, questions: list[Question]) -> str:
    return Template(settings.LABELING_SYSTEM_PROMPT).substitute(
        report=report_body,
        questions=_format_question_lines(questions),
    )


def sanitize_label(label: str) -> str:
    s = re.sub(r"[^A-Za-z0-9]", "_", label).lower()
    if not s or s[0].isdigit():
        s = "_" + s
    return s


def build_yes_no_maybe_schema(questions: list[Question]) -> type[BaseModel]:
    fields: dict[str, tuple[type, object]] = {}
    seen: dict[str, str] = {}
    for q in questions:
        key = sanitize_label(q.label)
        if key in seen:
            raise ValueError(
                f"Sanitized labels collide: {seen[key]!r} and {q.label!r} → {key!r}"
            )
        seen[key] = q.label
        fields[key] = (Literal["YES", "NO", "MAYBE"], ...)
    return create_model(
        "LabelingAnswers",
        __config__=ConfigDict(extra="forbid"),
        **fields,
    )
