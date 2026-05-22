from unittest.mock import patch

import pytest

from radis.core.models import AnalysisJob, AnalysisTask
from radis.labels.factories import LabelingJobFactory, LabelingTaskFactory, QuestionFactory
from radis.labels.models import Answer
from radis.labels.processors import LabelingTaskProcessor
from radis.reports.factories import ReportFactory


def _task_with_reports(n_reports=3):
    job = LabelingJobFactory(status=AnalysisJob.Status.PENDING)
    task = LabelingTaskFactory(job=job, status=AnalysisTask.Status.PENDING)
    for i in range(n_reports):
        task.reports.add(ReportFactory(body=f"body-{i}"))
    return task


@pytest.mark.django_db(transaction=True)
class TestLabelingTaskProcessor:
    def test_success_when_all_succeed(self):
        task = _task_with_reports(3)
        QuestionFactory(active=True, group="g")
        with patch("radis.labels.services.ChatClient") as ChatClientMock:
            ChatClientMock.return_value.extract_data.side_effect = lambda p, S: S(
                **{f: "YES" for f in S.model_fields}
            )
            LabelingTaskProcessor(task).start()
        task.refresh_from_db()
        assert task.status == AnalysisTask.Status.SUCCESS
        assert Answer.objects.count() == 3

    def test_warning_on_partial_failure(self):
        task = _task_with_reports(2)
        report_ids = list(task.reports.values_list("id", flat=True))
        QuestionFactory(active=True, group="g")
        from radis.reports.models import Report

        bad_id = report_ids[0]
        bad_body = Report.objects.get(id=bad_id).body

        def fake(prompt, Schema):
            if bad_body in prompt:
                raise RuntimeError("bad")
            return Schema(**{f: "YES" for f in Schema.model_fields})

        with patch("radis.labels.services.ChatClient") as ChatClientMock:
            ChatClientMock.return_value.extract_data.side_effect = fake
            LabelingTaskProcessor(task).start()
        task.refresh_from_db()
        assert task.status == AnalysisTask.Status.WARNING

    def test_failure_when_all_fail(self):
        task = _task_with_reports(2)
        QuestionFactory(active=True, group="g")
        with patch("radis.labels.services.ChatClient") as ChatClientMock:
            ChatClientMock.return_value.extract_data.side_effect = RuntimeError
            LabelingTaskProcessor(task).start()
        task.refresh_from_db()
        assert task.status == AnalysisTask.Status.FAILURE
