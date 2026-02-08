import pytest

from radis.labels.models import LabelChoice, LabelGroup, LabelQuestion
from radis.labels.utils.processor_utils import generate_questions_for_prompt


@pytest.mark.django_db
def test_generate_questions_for_prompt_includes_choices():
    group = LabelGroup.objects.create(name="Finding", slug="finding")
    question = LabelQuestion.objects.create(
        group=group,
        name="pulmonary_embolism",
        question="Pulmonary embolism present?",
    )
    LabelChoice.objects.create(question=question, value="yes", label="Yes", order=1)
    LabelChoice.objects.create(question=question, value="no", label="No", order=2)
    LabelChoice.objects.create(
        question=question,
        value="unknown",
        label="Unknown",
        is_unknown=True,
        order=3,
    )

    prompt = generate_questions_for_prompt([question])

    assert "question_0" in prompt
    assert "Pulmonary embolism present?" in prompt
    assert "yes (Yes)" in prompt
    assert "no (No)" in prompt
    assert "unknown (Unknown)" in prompt
