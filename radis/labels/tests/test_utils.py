import pytest
from pydantic import ValidationError

from radis.labels.models import Question, QuestionSet
from radis.labels.schemas import (
    build_answer_schema,
    question_set_from_orm,
    render_questions_for_prompt,
)


@pytest.mark.django_db
def test_render_questions_for_prompt_includes_options():
    question_set = QuestionSet.objects.create(name="Finding")
    Question.objects.create(
        question_set=question_set,
        label="Pulmonary embolism",
        question="Pulmonary embolism present?",
    )
    question_set.refresh_from_db()
    schema = question_set_from_orm(
        QuestionSet.objects.prefetch_related("questions__options").get(pk=question_set.pk)
    )

    prompt = render_questions_for_prompt(schema)

    assert "question_0" in prompt
    assert "Pulmonary embolism present?" in prompt
    assert "yes (Yes)" in prompt
    assert "no (No)" in prompt
    assert "cannot_decide (Cannot decide)" in prompt


@pytest.mark.django_db
def test_question_auto_generates_prompt_from_label():
    question_set = QuestionSet.objects.create(name="Finding")
    question = Question.objects.create(
        question_set=question_set,
        label="Pulmonary embolism",
        question="",
    )

    assert question.question == "Pulmonary embolism"


@pytest.mark.django_db
def test_build_answer_schema_enforces_option_enum():
    question_set = QuestionSet.objects.create(name="Finding")
    Question.objects.create(
        question_set=question_set,
        label="Broken bones",
        question="Is there a case of broken bones on this report?",
    )
    schema_mirror = question_set_from_orm(
        QuestionSet.objects.prefetch_related("questions__options").get(pk=question_set.pk)
    )

    Schema = build_answer_schema(schema_mirror)

    Schema.model_validate({"question_0": {"choice": "cannot_decide"}})

    with pytest.raises(ValidationError):
        Schema.model_validate({"question_0": {"choice": "maybe"}})
