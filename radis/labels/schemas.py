"""Canonical Pydantic representations of the labelling data model.

These mirrors are the single source of truth for everything that crosses a
process or storage boundary: LLM call schemas, eval seed files, dump/restore
of question sets between environments. The Django models stay the system of
record for persistence; these schemas describe shape and validate input.

The Django ORM and Pydantic representations are kept aligned by two
adapters per model (``from_orm`` and ``to_orm_defaults``) so callers don't
re-derive field mappings ad-hoc.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, create_model


class AnswerOptionSchema(BaseModel):
    """One allowed answer value for a question.

    ``value`` is the canonical enum token sent to and received from the LLM.
    ``label`` is the human display string. ``is_unknown`` marks the fallback
    used when the LLM cannot decide; exactly one option per question should
    set it.
    """

    model_config = ConfigDict(from_attributes=True)

    value: str = Field(min_length=1, max_length=50)
    label: str = Field(min_length=1, max_length=100)
    is_unknown: bool = False
    order: int = 0


class QuestionSchema(BaseModel):
    """A single question with its allowed answer options.

    ``label`` is the short identifier shown to staff; ``question`` is the
    full prompt-facing text. Both are passed to the LLM. ``version`` is
    advisory in this schema — it is set by the ORM but echoed here for
    round-trip serialization.
    """

    model_config = ConfigDict(from_attributes=True)

    label: str = Field(min_length=1, max_length=200)
    question: str = Field(default="", max_length=300)
    is_active: bool = True
    order: int = 0
    version: int = 1
    options: list[AnswerOptionSchema] = Field(default_factory=list)


class QuestionSetSchema(BaseModel):
    """A coherent set of questions answered together per report.

    This is the canonical representation. Seeds load from YAML/JSON into
    this shape; staff edits round-trip through it; LLM prompt construction
    consumes it. The :func:`build_answer_schema` helper below converts an
    instance into the strict Pydantic schema the LLM responds with.
    """

    model_config = ConfigDict(from_attributes=True)

    name: str = Field(min_length=1, max_length=100)
    description: str = ""
    is_active: bool = True
    order: int = 0
    questions: list[QuestionSchema] = Field(default_factory=list)


# --- LLM response schemas --------------------------------------------------


def _build_single_answer_schema(index: int, question: QuestionSchema) -> type[BaseModel]:
    """Build the strict Pydantic answer schema for one question.

    ``choice`` is a ``Literal`` over the question's option values so OpenAI
    structured output cannot return anything outside the configured set. The
    LLM still returns confidence and rationale as free-form to help us judge
    output quality and aid evaluation; both are optional because some LLMs
    decline to set them under terse prompts.
    """
    option_values = tuple(
        option.value
        for option in question.options
        if isinstance(option.value, str) and option.value
    )
    if not option_values:
        raise ValueError(
            f"Question '{question.label}' has no valid answer options configured."
        )

    ChoiceType = Literal[*option_values]
    return create_model(
        f"Answer_{index}",
        choice=(ChoiceType, ...),
        confidence=(float | None, None),
        rationale=(str | None, None),
    )


def build_answer_schema(question_set: QuestionSetSchema) -> type[BaseModel]:
    """Build the Pydantic schema the LLM must conform to for this set.

    Each active question becomes a field ``question_{index}`` whose value is
    a single-answer model with ``choice`` constrained to the question's
    option values via ``Literal``. Inactive questions are skipped — the
    schema only covers what we ask in this run.
    """
    field_definitions: dict[str, Any] = {}
    active_questions = [q for q in question_set.questions if q.is_active]
    if not active_questions:
        raise ValueError(
            f"QuestionSet '{question_set.name}' has no active questions to label."
        )

    for index, question in enumerate(active_questions):
        AnswerSchema = _build_single_answer_schema(index, question)
        field_definitions[f"question_{index}"] = (AnswerSchema, ...)

    return create_model(f"LabelingResponse_{question_set.name}", **field_definitions)


def render_questions_for_prompt(question_set: QuestionSetSchema) -> str:
    """Render a human/LLM-readable block listing each active question and its options.

    Substituted into ``$questions`` in the labelling system prompts.
    """
    lines: list[str] = []
    active_questions = [q for q in question_set.questions if q.is_active]
    for index, question in enumerate(active_questions):
        options_text = ", ".join(
            f"{option.value} ({option.label})" for option in question.options
        )
        lines.append(f"question_{index}: {question.question}")
        lines.append(f"choices (return exactly one choice value): {options_text}")
    return "\n".join(lines) + ("\n" if lines else "")


# --- ORM adapters ----------------------------------------------------------


def question_set_from_orm(question_set) -> QuestionSetSchema:
    """Materialize a ``QuestionSet`` ORM row (with prefetched questions/options)
    as the canonical schema. Inactive questions are kept so the schema is a
    faithful representation; the LLM-facing helpers above filter to active.
    """
    questions: list[QuestionSchema] = []
    for question in question_set.questions.all():
        options = [
            AnswerOptionSchema(
                value=option.value,
                label=option.label,
                is_unknown=option.is_unknown,
                order=option.order,
            )
            for option in question.options.all()
        ]
        questions.append(
            QuestionSchema(
                label=question.label,
                question=question.question,
                is_active=question.is_active,
                order=question.order,
                version=question.version,
                options=options,
            )
        )

    return QuestionSetSchema(
        name=question_set.name,
        description=question_set.description,
        is_active=question_set.is_active,
        order=question_set.order,
        questions=questions,
    )
