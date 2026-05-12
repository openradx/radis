"""Thin wrappers that adapt the canonical Pydantic schemas to the labelling
processor. The actual schema construction lives in ``radis.labels.schemas``;
this module exists so the processor only has to import one place.
"""

from __future__ import annotations

from pydantic import BaseModel

from ..models import QuestionSet
from ..schemas import (
    QuestionSetSchema,
    build_answer_schema,
    question_set_from_orm,
    render_questions_for_prompt,
)


def question_set_schema_for_run(question_set: QuestionSet) -> QuestionSetSchema:
    """Build the Pydantic mirror of a ``QuestionSet`` for use in one LLM run.

    The caller is responsible for prefetching ``questions__options`` before
    invoking this; we do not re-query inside to keep N+1 obvious at the call
    site.
    """
    return question_set_from_orm(question_set)


def build_labeling_response_schema(schema: QuestionSetSchema) -> type[BaseModel]:
    return build_answer_schema(schema)


def render_questions_block(schema: QuestionSetSchema) -> str:
    return render_questions_for_prompt(schema)
