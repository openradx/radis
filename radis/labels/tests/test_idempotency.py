from datetime import timedelta

from radis.labels.factories import AnswerFactory, QuestionFactory
from radis.labels.models import Answer, Question
from radis.labels.services import _group_answers_are_current
from radis.reports.factories import ReportFactory


def _existing(report):
    return {
        a.question_id: a for a in Answer.objects.filter(report=report).select_related("question")
    }


class TestGroupAnswersAreCurrent:
    def test_all_current(self):
        r = ReportFactory()
        q = QuestionFactory()
        a = AnswerFactory(report=r, question=q)
        Question.objects.filter(pk=q.pk).update(updated_at=a.generated_at - timedelta(seconds=1))
        r.refresh_from_db()
        q.refresh_from_db()
        assert _group_answers_are_current([q], _existing(r), r.updated_at) is True

    def test_missing_answer_means_not_current(self):
        r = ReportFactory()
        q = QuestionFactory()
        assert _group_answers_are_current([q], _existing(r), r.updated_at) is False

    def test_question_edited_after_answer(self):
        r = ReportFactory()
        q = QuestionFactory()
        a = AnswerFactory(report=r, question=q)
        future = a.generated_at + timedelta(seconds=10)
        Question.objects.filter(pk=q.pk).update(updated_at=future)
        q.refresh_from_db()
        assert _group_answers_are_current([q], _existing(r), r.updated_at) is False

    def test_report_updated_after_answer(self):
        r = ReportFactory()
        q = QuestionFactory()
        a = AnswerFactory(report=r, question=q)
        future = a.generated_at + timedelta(seconds=10)
        type(r).objects.filter(pk=r.pk).update(updated_at=future)
        r.refresh_from_db()
        assert _group_answers_are_current([q], _existing(r), r.updated_at) is False
