import pytest
from pydantic import ValidationError

from radis.labels.models import LabelGroup, LabelQuestion
from radis.labels.utils.processor_utils import (
    generate_labeling_schema,
    generate_questions_for_prompt,
)


@pytest.mark.django_db
def test_generate_questions_for_prompt_includes_choices():
    group = LabelGroup.objects.create(name="Finding")
    question = LabelQuestion.objects.create(
        group=group,
        label="Pulmonary embolism",
        question="Pulmonary embolism present?",
    )
    choices = list(question.choices.all())

    prompt = generate_questions_for_prompt([question])

    assert "question_0" in prompt
    assert "Pulmonary embolism present?" in prompt
    assert any(choice.value in prompt for choice in choices)
    assert "yes (Yes)" in prompt
    assert "no (No)" in prompt
    assert "cannot_decide (Cannot decide)" in prompt


@pytest.mark.django_db
def test_label_question_auto_generates_prompt_from_label():
    group = LabelGroup.objects.create(name="Finding")
    question = LabelQuestion.objects.create(
        group=group,
        label="Pulmonary embolism",
        question="",
    )

    assert question.question == "Pulmonary embolism"


@pytest.mark.django_db
def test_generate_labeling_schema_enforces_choice_enum():
    group = LabelGroup.objects.create(name="Finding")
    question = LabelQuestion.objects.create(
        group=group,
        label="Broken bones",
        question="Is there a case of broken bones on this report?",
    )

    Schema = generate_labeling_schema([question])

    # Valid values should validate.
    Schema.model_validate({"question_0": {"choice": "cannot_decide"}})

    # Anything outside the configured choice values should fail validation.
    with pytest.raises(ValidationError):
        Schema.model_validate({"question_0": {"choice": "maybe"}})
