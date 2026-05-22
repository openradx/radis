import pytest
from django.db import IntegrityError

from radis.core.models import AnalysisJob, AnalysisTask
from radis.labels.factories import (
    AnswerFactory,
    LabelingJobFactory,
    LabelingTaskFactory,
    QuestionFactory,
)
from radis.labels.models import Answer, LabelingJob, LabelingTask, Question
from radis.reports.factories import ReportFactory


class TestQuestion:
    def test_str_returns_label(self):
        q = QuestionFactory(label="pneumonia")
        assert str(q) == "pneumonia"

    def test_default_active_is_true(self):
        assert QuestionFactory().active is True

    def test_label_is_unique(self):
        QuestionFactory(label="pneumonia")
        with pytest.raises(IntegrityError):
            QuestionFactory(label="pneumonia")

    def test_updated_at_advances_on_save(self):
        q = QuestionFactory()
        before = q.updated_at
        q.text = "edited"
        q.save()
        q.refresh_from_db()
        assert q.updated_at > before


class TestAnswer:
    def test_value_choices(self):
        assert set(Answer.Value.values) == {"YES", "NO", "MAYBE"}

    def test_unique_per_report_question(self):
        report = ReportFactory()
        q = QuestionFactory()
        AnswerFactory(report=report, question=q, value="YES")
        with pytest.raises(IntegrityError):
            AnswerFactory(report=report, question=q, value="NO")

    def test_generated_at_bumps_on_save(self):
        a = AnswerFactory(value="YES")
        before = a.generated_at
        a.value = "MAYBE"
        a.save()
        a.refresh_from_db()
        assert a.generated_at > before

    def test_cascade_with_question(self):
        a = AnswerFactory()
        q_id = a.question.id
        a.question.delete()
        assert not Answer.objects.filter(question_id=q_id).exists()

    def test_cascade_with_report(self):
        a = AnswerFactory()
        r_id = a.report.id
        a.report.delete()
        assert not Answer.objects.filter(report_id=r_id).exists()


class TestLabelingJobModel:
    def test_inherits_from_analysis_job(self):
        assert issubclass(LabelingJob, AnalysisJob)

    def test_active_statuses_constant(self):
        assert AnalysisJob.Status.PREPARING in LabelingJob.ACTIVE_STATUSES
        assert AnalysisJob.Status.IN_PROGRESS in LabelingJob.ACTIVE_STATUSES
        assert AnalysisJob.Status.SUCCESS not in LabelingJob.ACTIVE_STATUSES


class TestLabelingTaskModel:
    def test_inherits_from_analysis_task(self):
        assert issubclass(LabelingTask, AnalysisTask)

    def test_reports_m2m(self):
        r = ReportFactory()
        t = LabelingTaskFactory()
        t.reports.add(r)
        assert r in t.reports.all()
