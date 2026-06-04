import pytest

from radis.core.models import AnalysisTask
from radis.labels.factories import LabelingJobFactory, LabelingTaskFactory
from radis.labels.models import LabelingJob
from radis.reports.factories import ReportFactory


@pytest.mark.django_db(transaction=True)
def test_processor_calls_label_report_for_each_report(monkeypatch):
    from radis.labels import processors

    called = []
    monkeypatch.setattr(processors, "label_report", lambda rid: called.append(rid))

    job = LabelingJobFactory.create(status=LabelingJob.Status.PENDING)
    task = LabelingTaskFactory.create(job=job, status=AnalysisTask.Status.PENDING)
    r1, r2 = ReportFactory.create(), ReportFactory.create()
    task.reports.add(r1, r2)

    processors.LabelingTaskProcessor(task).start()

    task.refresh_from_db()
    assert set(called) == {r1.pk, r2.pk}
    assert task.status == AnalysisTask.Status.SUCCESS


@pytest.mark.django_db(transaction=True)
def test_processor_one_report_failure_yields_warning(monkeypatch):
    from radis.labels import processors

    r_ok, r_bad = ReportFactory.create(), ReportFactory.create()

    def fake_label_report(rid):
        if rid == r_bad.pk:
            raise RuntimeError("LLM exploded")

    monkeypatch.setattr(processors, "label_report", fake_label_report)

    job = LabelingJobFactory.create(status=LabelingJob.Status.PENDING)
    task = LabelingTaskFactory.create(job=job, status=AnalysisTask.Status.PENDING)
    task.reports.add(r_ok, r_bad)

    processors.LabelingTaskProcessor(task).start()

    task.refresh_from_db()
    assert task.status == AnalysisTask.Status.WARNING
    assert task.message  # a human-readable reason is recorded
