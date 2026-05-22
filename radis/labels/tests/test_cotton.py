from datetime import timedelta

from django.template import Context, Template
from django_cotton.compiler_regex import CottonCompiler

from radis.reports.factories import ReportFactory
from radis.labels.factories import AnswerFactory, QuestionFactory
from radis.labels.models import Answer, Question

_COTTON_COMPILER = CottonCompiler()


def _render(report):
    source = '{% load cotton %}<c-report-labels :report="report" />'
    compiled = _COTTON_COMPILER.process(source)
    tmpl = Template(compiled)
    return tmpl.render(Context({"report": report}))


class TestReportLabelsComponent:
    def test_yes_badge(self):
        r = ReportFactory()
        q = QuestionFactory(label="pneumonia", group="lung")
        AnswerFactory(report=r, question=q, value="YES")
        out = _render(r)
        assert "pneumonia" in out

    def test_maybe_marked_distinctly(self):
        r = ReportFactory()
        q = QuestionFactory(label="pneumonia", group="lung")
        AnswerFactory(report=r, question=q, value="MAYBE")
        out = _render(r)
        assert "pneumonia" in out
        assert "?" in out or "maybe" in out.lower()

    def test_no_value_not_rendered(self):
        r = ReportFactory()
        q = QuestionFactory(label="not-applicable", group="lung")
        AnswerFactory(report=r, question=q, value="NO")
        out = _render(r)
        assert "not-applicable" not in out

    def test_stale_styling(self):
        r = ReportFactory()
        q = QuestionFactory(label="pneumonia", group="lung")
        a = AnswerFactory(report=r, question=q, value="YES")
        Question.objects.filter(pk=q.pk).update(
            updated_at=a.generated_at + timedelta(seconds=10)
        )
        out = _render(r)
        assert "stale" in out.lower() or "outdated" in out.lower()

    def test_no_answers_shows_pending(self):
        r = ReportFactory()
        out = _render(r)
        assert "pending" in out.lower()
