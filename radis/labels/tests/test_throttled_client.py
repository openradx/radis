"""label_report runs the real ChatClient + gate with only openai.OpenAI mocked, so a 429 or a
transient error is handled end-to-end (retry/defer) and results still persist."""

from unittest.mock import MagicMock, patch

import pytest

from radis.chats.utils.rate_limit import RateLimited
from radis.chats.utils.testing_helpers import make_connection_error, make_rate_limit_error
from radis.labels.factories import LabelFactory, LabelGroupFactory
from radis.labels.labeling import label_report
from radis.labels.models import GateAnswer, LabelResult
from radis.labels.tests.helpers import create_labeling_openai_mock
from radis.labels.throttled_client import _LABELING_GATE
from radis.reports.factories import ReportFactory


@pytest.fixture(autouse=True)
def reset_labeling_gate():
    _LABELING_GATE.reset()
    yield
    _LABELING_GATE.reset()


def _inject_failures(client_mock: MagicMock, error: Exception, times: int) -> MagicMock:
    """Make the first `times` parse calls raise `error`, then behave normally."""
    normal = client_mock.beta.chat.completions.parse.side_effect
    state = {"left": times}

    def flaky(**kwargs):
        if state["left"] > 0:
            state["left"] -= 1
            raise error
        return normal(**kwargs)

    client_mock.beta.chat.completions.parse.side_effect = flaky
    return client_mock


@pytest.mark.django_db
def test_label_report_recovers_after_one_rate_limit():
    report = ReportFactory.create(body="CT thorax: consolidation right lower lobe.")
    group = LabelGroupFactory.create(name="Chest")
    label = LabelFactory.create(group=group, name="pneumonia")

    client = create_labeling_openai_mock(
        gate_values={group.name: "YES"}, label_values={label.name: "PRESENT"}
    )
    _inject_failures(client, make_rate_limit_error({"retry-after": "0"}), times=1)

    with patch("openai.OpenAI", return_value=client):
        label_report(report.pk)

    assert LabelResult.objects.get(report=report, label=label).value == LabelResult.Value.PRESENT


@pytest.mark.django_db
def test_label_report_recovers_after_one_transient_error(settings):
    settings.LABELING_TRANSIENT_RETRY_BASE_SECONDS = 0.0  # no real sleep in the test
    report = ReportFactory.create(body="MRI head: no acute infarct.")
    group = LabelGroupFactory.create(name="Neuro")
    label = LabelFactory.create(group=group, name="stroke")

    client = create_labeling_openai_mock(
        gate_values={group.name: "YES"}, label_values={label.name: "ABSENT"}
    )
    _inject_failures(client, make_connection_error(), times=1)

    with patch("openai.OpenAI", return_value=client):
        label_report(report.pk)

    assert LabelResult.objects.get(report=report, label=label).value == LabelResult.Value.ABSENT


@pytest.mark.django_db
def test_label_report_defers_when_rate_limit_exceeds_budget():
    report = ReportFactory.create(body="CT abdomen: normal.")
    group = LabelGroupFactory.create(name="Abdomen")
    LabelFactory.create(group=group, name="appendicitis")

    client = create_labeling_openai_mock(gate_values={group.name: "YES"})
    # retry-after 600 > 300 budget -> give up on the first call.
    _inject_failures(client, make_rate_limit_error({"retry-after": "600"}), times=1)

    with patch("openai.OpenAI", return_value=client):
        with pytest.raises(RateLimited):
            label_report(report.pk)

    assert not GateAnswer.objects.filter(report=report).exists()
    assert not LabelResult.objects.filter(report=report).exists()
