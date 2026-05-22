from datetime import timedelta

from radis.reports.factories import ReportFactory
from radis.reports.models import Report
from radis.labels.factories import AnswerFactory, QuestionFactory
from radis.labels.models import Question
from radis.labels.services import find_reports_needing_work


class TestFindReportsNeedingWork:
    def test_no_active_questions_means_empty_scope(self):
        ReportFactory()
        QuestionFactory(active=False)
        assert list(find_reports_needing_work(Report.objects.values_list("id", flat=True))) == []

    def test_report_with_no_answers_in_scope(self):
        r = ReportFactory()
        QuestionFactory(active=True)
        ids = list(find_reports_needing_work([r.id]))
        assert ids == [r.id]

    def test_report_with_all_current_answers_out_of_scope(self):
        r = ReportFactory()
        q1 = QuestionFactory(active=True)
        q2 = QuestionFactory(active=True)
        AnswerFactory(report=r, question=q1)
        AnswerFactory(report=r, question=q2)
        ids = list(find_reports_needing_work([r.id]))
        assert ids == []

    def test_report_with_stale_answer_in_scope(self):
        r = ReportFactory()
        q = QuestionFactory(active=True)
        a = AnswerFactory(report=r, question=q)
        Question.objects.filter(pk=q.pk).update(
            updated_at=a.generated_at + timedelta(seconds=10)
        )
        ids = list(find_reports_needing_work([r.id]))
        assert ids == [r.id]
