"""Runs the real ChatClient + schema/prompt builders with only openai.OpenAI mocked (the unit
suite fakes that), pinning the prompt/schema sent on the wire and the gate→label flow."""

from unittest.mock import patch

import pytest

from radis.labels.factories import LabelFactory, LabelGroupFactory
from radis.labels.labeling import label_report
from radis.labels.models import GateAnswer, LabelResult
from radis.labels.tests.helpers import create_labeling_openai_mock
from radis.labels.utils.prompts import render_gate_prompt, render_label_prompt
from radis.reports.factories import ReportFactory


@pytest.mark.django_db
def test_real_chat_client_two_phase_flow_persists_results():
    """Gate YES drives per-label classification; both phases persist through the real client."""
    report = ReportFactory.create(body="CT thorax: dense consolidation in the right lower lobe.")
    group = LabelGroupFactory.create(name="Chest")
    present = LabelFactory.create(group=group, name="pneumonia")
    absent = LabelFactory.create(group=group, name="pneumothorax")

    client = create_labeling_openai_mock(
        gate_values={group.name: "YES"},
        label_values={present.name: "PRESENT", absent.name: "ABSENT"},
    )
    with patch("openai.OpenAI", return_value=client):
        label_report(report.pk)

    assert GateAnswer.objects.get(report=report, label_group=group).value == GateAnswer.Value.YES
    assert LabelResult.objects.get(report=report, label=present).value == LabelResult.Value.PRESENT
    assert LabelResult.objects.get(report=report, label=absent).value == LabelResult.Value.ABSENT
    # Exactly one gate parse and one label parse — no redundant calls.
    assert client.beta.chat.completions.parse.call_count == 2


@pytest.mark.django_db
def test_real_chat_client_sends_rendered_prompt_and_built_schema():
    """The prompt and response_format on the wire are exactly what radis.labels builds."""
    report = ReportFactory.create(body="MRI head: no acute infarct.")
    group = LabelGroupFactory.create(name="Neuro", gate_question="Is this a head study?")
    label = LabelFactory.create(
        group=group, name="ischemic stroke", description="acute or chronic infarction"
    )

    client = create_labeling_openai_mock(
        gate_values={group.name: "YES"},
        label_values={label.name: "ABSENT"},
    )
    with patch("openai.OpenAI", return_value=client):
        label_report(report.pk)

    calls = client.beta.chat.completions.parse.call_args_list
    gate_kwargs = next(
        c.kwargs for c in calls if c.kwargs["response_format"].__name__ == "GateScreening"
    )
    label_kwargs = next(
        c.kwargs for c in calls if c.kwargs["response_format"].__name__ == "LabelClassification"
    )

    # The system message sent is exactly the labels code's rendered prompt for this report body.
    assert gate_kwargs["messages"] == [
        {"role": "system", "content": render_gate_prompt(report.body)}
    ]
    assert label_kwargs["messages"] == [
        {"role": "system", "content": render_label_prompt(report.body)}
    ]

    # response_format is the dynamically-built schema: name-keyed, carrying the gate question /
    # label definition the LLM is asked to honor.
    gate_schema = gate_kwargs["response_format"]
    assert set(gate_schema.model_fields) == {group.name}
    assert gate_schema.model_fields[group.name].description == group.gate_question

    label_schema = label_kwargs["response_format"]
    assert set(label_schema.model_fields) == {label.name}
    assert label_schema.model_fields[label.name].description == label.description


@pytest.mark.django_db
def test_real_chat_client_gate_no_skips_label_classification():
    """A NO gate short-circuits: one gate call, no label call, no results — via the real client."""
    report = ReportFactory.create(body="Abdomen ultrasound: unremarkable.")
    group = LabelGroupFactory.create(name="Chest")
    label = LabelFactory.create(group=group, name="pneumonia")

    client = create_labeling_openai_mock(gate_values={group.name: "NO"})
    with patch("openai.OpenAI", return_value=client):
        label_report(report.pk)

    calls = client.beta.chat.completions.parse.call_args_list
    assert len(calls) == 1
    assert calls[0].kwargs["response_format"].__name__ == "GateScreening"
    assert GateAnswer.objects.get(report=report, label_group=group).value == GateAnswer.Value.NO
    assert not LabelResult.objects.filter(report=report, label=label).exists()
