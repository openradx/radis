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


@pytest.mark.django_db(transaction=True)
def test_processor_persists_per_report_failures_to_log(monkeypatch):
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
    assert task.message == "1 of 2 reports failed to label."
    assert f"Report {r_bad.pk}: RuntimeError: LLM exploded" in task.log
    assert f"Report {r_ok.pk}:" not in task.log


@pytest.mark.django_db(transaction=True)
def test_processor_truncates_large_failure_log(monkeypatch):
    from radis.labels import processors

    monkeypatch.setattr(processors, "_MAX_LOGGED_FAILURES", 2)

    def fake_label_report(rid):
        raise RuntimeError("boom")

    monkeypatch.setattr(processors, "label_report", fake_label_report)

    reports = [ReportFactory.create() for _ in range(4)]
    job = LabelingJobFactory.create(status=LabelingJob.Status.PENDING)
    task = LabelingTaskFactory.create(job=job, status=AnalysisTask.Status.PENDING)
    task.reports.add(*reports)

    processors.LabelingTaskProcessor(task).start()

    task.refresh_from_db()
    assert task.status == AnalysisTask.Status.WARNING
    assert task.message == "4 of 4 reports failed to label."
    assert task.log.count("Report ") == 2
    assert "… and 2 more" in task.log
