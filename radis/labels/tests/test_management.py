from io import StringIO

from django.core.management import call_command

from radis.labels.factories import AnswerFactory, QuestionFactory
from radis.reports.factories import ReportFactory


def test_labels_status_prints_coverage():
    ReportFactory()
    q = QuestionFactory(label="pneumonia", active=True)
    AnswerFactory(question=q, value="YES")
    buf = StringIO()
    call_command("labels_status", stdout=buf)
    out = buf.getvalue()
    assert "pneumonia" in out
    assert "total" in out.lower()
