from datetime import timedelta

from radis.core.models import AnalysisJob, AnalysisTask
from radis.labels.factories import AnswerFactory, LabelingJobFactory, QuestionFactory
from radis.labels.models import LabelingTask, Question
from radis.labels.services import create_labeling_tasks_streaming, find_reports_needing_work
from radis.reports.factories import ReportFactory
from radis.reports.models import Report


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
        Question.objects.filter(pk=q.pk).update(updated_at=a.generated_at + timedelta(seconds=10))
        ids = list(find_reports_needing_work([r.id]))
        assert ids == [r.id]


def test_create_labeling_tasks_streaming_buckets_by_batch_size(settings):
    settings.LABELING_TASK_BATCH_SIZE = 3
    job = LabelingJobFactory(status=AnalysisJob.Status.PREPARING)
    QuestionFactory(active=True)
    for _ in range(7):
        ReportFactory()
    create_labeling_tasks_streaming(job)
    tasks = list(LabelingTask.objects.filter(job=job).order_by("id"))
    assert [t.reports.count() for t in tasks] == [3, 3, 1]
    assert all(t.status == AnalysisTask.Status.PENDING for t in tasks)


def test_create_labeling_tasks_streaming_aborts_on_cancel(settings):
    settings.LABELING_TASK_BATCH_SIZE = 2
    job = LabelingJobFactory(status=AnalysisJob.Status.PREPARING)
    QuestionFactory(active=True)
    for _ in range(10):
        ReportFactory()

    from radis.labels import services as services_mod

    real_flush = services_mod._flush_bucket

    def cancel_after_first(job, ids):
        real_flush(job, ids)
        type(job).objects.filter(pk=job.pk).update(status=AnalysisJob.Status.CANCELING)

    from unittest.mock import patch

    with patch.object(services_mod, "_flush_bucket", side_effect=cancel_after_first):
        create_labeling_tasks_streaming(job)
    # Only the first bucket got flushed before cancellation was observed.
    assert LabelingTask.objects.filter(job=job).count() == 1
