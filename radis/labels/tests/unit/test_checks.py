import pytest
from django.test import override_settings

from radis.labels.apps import check_labeling_prompt_placeholders


def test_default_prompt_passes():
    assert check_labeling_prompt_placeholders(None) == []


@override_settings(LABELING_SYSTEM_PROMPT="Labels: ${labels}\nReport: ${report}")
def test_braced_placeholders_pass():
    assert check_labeling_prompt_placeholders(None) == []


@pytest.mark.parametrize(
    ("prompt", "missing"),
    [
        ("Classify this report: $report", "$labels"),
        ("Classify against: $labels", "$report"),
        ("Classify the report.", "$labels, $report"),
    ],
)
def test_missing_placeholder_is_an_error(prompt, missing):
    with override_settings(LABELING_SYSTEM_PROMPT=prompt):
        errors = check_labeling_prompt_placeholders(None)
    assert [e.id for e in errors] == ["labels.E001"]
    assert errors[0].msg.endswith(f"{missing}.")
