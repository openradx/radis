from unittest.mock import patch

import pytest

from radis.labels.factories import QuestionFactory
from radis.labels.models import Answer
from radis.labels.services import label_reports_in_parallel
from radis.reports.factories import ReportFactory


@pytest.mark.django_db(transaction=True)
def test_returns_success_and_failure_counts():
    r1 = ReportFactory(body="ok 1")
    r2 = ReportFactory(body="ok 2")
    r3 = ReportFactory(body="boom")
    QuestionFactory(label="x", group="g", active=True)

    def fake_extract(prompt, Schema):
        if "boom" in prompt:
            raise RuntimeError("LLM down")
        return Schema(**{f: "YES" for f in Schema.model_fields})

    with patch("radis.labels.services.ChatClient") as ChatClientMock:
        ChatClientMock.return_value.extract_data.side_effect = fake_extract
        ok, fail = label_reports_in_parallel([r1.id, r2.id, r3.id])
    assert ok == 2 and fail == 1
    assert Answer.objects.filter(report=r1).exists()
    assert Answer.objects.filter(report=r2).exists()
    assert not Answer.objects.filter(report=r3).exists()
