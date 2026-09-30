from unittest.mock import patch

from django.test import override_settings

from radis.core.utils.model_spec import ModelSpec
from radis.labels_lab.baseline import run_llm_baseline
from radis.labels_lab.questions import GroupSpec, LabelSpec
from radis.labels_lab.tests.helpers import FakeLLM

CHEST = GroupSpec(
    id=7,
    name="Chest pathology",
    gate_question="Does this report describe imaging of the chest?",
    labels=(
        LabelSpec(id=3, name="pneumonia", description="Infectious consolidation of the lung."),
        LabelSpec(id=4, name="pneumothorax", description="Air in the pleural space."),
    ),
)
ABDOMEN = GroupSpec(
    id=8,
    name="Acute abdomen",
    gate_question="Does this report describe imaging of the abdomen or pelvis?",
    labels=(LabelSpec(id=5, name="appendicitis", description="Inflamed appendix."),),
)
HEAD = GroupSpec(
    id=9,
    name="Neuroimaging",
    gate_question="Does this report describe imaging of the head?",
    labels=(),
)

GATE_VALUES = {"Chest pathology": "YES", "Acute abdomen": "NO", "Neuroimaging": "NO"}
LABEL_VALUES = {"pneumonia": "LIKELY", "pneumothorax": "ABSENT", "appendicitis": "UNMENTIONED"}


def _run(groups, body="Lungs are clear."):
    """Run the baseline against an LLM that answers with the values above."""
    client = FakeLLM(GATE_VALUES, LABEL_VALUES)
    with patch("radis.labels_lab.baseline.LLMClient", return_value=client):
        return run_llm_baseline(body, groups), client


def test_gate_answers_and_buckets_are_keyed_by_group_and_label_id():
    baseline, _ = _run([CHEST, ABDOMEN])

    assert baseline.gates == {7: "YES", 8: "NO"}
    assert baseline.buckets == {3: "LIKELY", 4: "ABSENT", 5: "UNMENTIONED"}


def test_labels_are_classified_even_when_their_gate_answers_no():
    _, client = _run([ABDOMEN])

    assert client.label_calls == [["appendicitis"]]


def test_a_group_without_labels_is_gated_but_not_classified():
    baseline, client = _run([HEAD])

    assert baseline.gates == {9: "NO"}
    assert client.label_calls == []


@override_settings(LABELING_GATE_BATCH_SIZE=2)
def test_gates_are_screened_in_batches_like_the_labeling_pipeline():
    _, client = _run([CHEST, ABDOMEN, HEAD])

    assert client.gate_calls == [["Chest pathology", "Acute abdomen"], ["Neuroimaging"]]


@override_settings(
    LABELING_GATE_SYSTEM_PROMPT="GATE PROMPT for: $report",
    LABELING_SYSTEM_PROMPT="LABEL PROMPT for: $report",
)
def test_every_llm_call_is_recorded_with_the_prompt_schema_and_output_it_used():
    baseline, _ = _run([CHEST], body="Lungs are clear.")

    gate_call, label_call = baseline.calls
    assert gate_call.purpose == "gate"
    assert gate_call.prompt == "GATE PROMPT for: Lungs are clear."
    assert gate_call.schema["properties"]["Chest pathology"]["description"] == (
        "Does this report describe imaging of the chest?"
    )
    assert gate_call.output == {"Chest pathology": "YES"}
    assert label_call.purpose == "labels: Chest pathology"
    assert label_call.prompt == "LABEL PROMPT for: Lungs are clear."
    assert set(label_call.schema["properties"]) == {"pneumonia", "pneumothorax"}
    assert label_call.output == {"pneumonia": "LIKELY", "pneumothorax": "ABSENT"}


@override_settings(LLM_MODELS={"labeling": ModelSpec("test-llm", {"temperature": 0})})
def test_the_configured_labeling_model_is_reported():
    baseline, _ = _run([CHEST])

    assert baseline.model == "test-llm"


@override_settings(LLM_RATE_LIMIT_INTERACTIVE_MAX_WAIT_SECONDS=7.0)
def test_llm_calls_wait_behind_the_rate_limit_only_as_long_as_a_user_would():
    _, client = _run([CHEST])

    assert client.max_waits == [7.0, 7.0]  # the gate call and the label call
