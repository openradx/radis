import json
from pathlib import Path

import pytest
from django.core.management import call_command

from radis.labels.models import LabelChoice, LabelGroup, LabelQuestion


@pytest.mark.django_db
def test_labels_seed_command_creates_objects(tmp_path: Path):
    payload = {
        "groups": [
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

    group = LabelGroup.objects.get(name="Embolism")
    question = LabelQuestion.objects.get(group=group, label="Pulmonary embolism")
    choices = LabelChoice.objects.filter(question=question)

    assert group.name == "Embolism"
    assert question.question == "Pulmonary embolism present?"
    assert choices.count() == 3
    assert choices.filter(value="cannot_decide", is_unknown=True).exists()
