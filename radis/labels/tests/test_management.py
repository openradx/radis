import json
from pathlib import Path

import pytest
from django.core.management import call_command

from radis.labels.models import AnswerOption, Question, QuestionSet


@pytest.mark.django_db
def test_labels_seed_command_creates_objects(tmp_path: Path):
    payload = {
        "question_sets": [
            {
                "name": "Embolism",
                "questions": [
                    {
                        "label": "Pulmonary embolism",
                        "question": "Pulmonary embolism present?",
                    }
                ],
            }
        ]
    }
    seed_file = tmp_path / "labels_seed.json"
    seed_file.write_text(json.dumps(payload))

    call_command("labels_seed", file=str(seed_file))

    question_set = QuestionSet.objects.get(name="Embolism")
    question = Question.objects.get(question_set=question_set, label="Pulmonary embolism")
    options = AnswerOption.objects.filter(question=question)

    assert question_set.name == "Embolism"
    assert question.question == "Pulmonary embolism present?"
    assert options.count() == 3
    assert options.filter(value="cannot_decide", is_unknown=True).exists()
