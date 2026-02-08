import pytest

from radis.labels.models import LabelGroup, LabelQuestion
from radis.labels.utils.processor_utils import generate_questions_for_prompt


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
