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
                "slug": "embolism",
                "name": "Embolism",
                "questions": [
                    {
                        "name": "pulmonary_embolism",
                        "question": "Pulmonary embolism present?",
                        "choices": [
                            {"value": "yes", "label": "Yes"},
                            {"value": "no", "label": "No"},
                            {
                                "value": "unknown",
                                "label": "Unknown",
                                "is_unknown": True,
                            },
                        ],
                    }
                ],
            }
        ]
    }
    seed_file = tmp_path / "labels_seed.json"
    seed_file.write_text(json.dumps(payload))

    call_command("labels_seed", file=str(seed_file))

    group = LabelGroup.objects.get(slug="embolism")
    question = LabelQuestion.objects.get(group=group, name="pulmonary_embolism")
    choices = LabelChoice.objects.filter(question=question)

    assert group.name == "Embolism"
    assert question.question == "Pulmonary embolism present?"
    assert choices.count() == 3
    assert choices.filter(value="unknown", is_unknown=True).exists()
