from unittest.mock import patch

import pytest
from adit_radis_shared.accounts.factories import UserFactory
from django.utils import timezone

from radis.core.models import AnalysisJob, AnalysisTask
from radis.extractions.factories import (
    ExtractionInstanceFactory,
    ExtractionJobFactory,
    ExtractionTaskFactory,
)
from radis.extractions.processors import ExtractionTaskProcessor
from radis.reports.models import Report


@pytest.mark.django_db
def test_resumed_task_skips_already_processed_instances():
    user = UserFactory.create()
    job = ExtractionJobFactory.create(owner=user, status=AnalysisJob.Status.IN_PROGRESS)
    task = ExtractionTaskFactory.create(job=job, status=AnalysisTask.Status.IN_PROGRESS)
    ExtractionInstanceFactory.create(task=task, is_processed=True)
    todo = ExtractionInstanceFactory.create(task=task, is_processed=False)

    with patch("radis.extractions.processors.LLMClient"):
        processor = ExtractionTaskProcessor(task)

    with patch.object(processor, "process_instance") as mock_process_instance:
        processor.process_task(task)

    assert mock_process_instance.call_count == 1
    assert mock_process_instance.call_args[0][0] == todo


@pytest.mark.django_db
def test_process_task_skips_instances_of_withdrawn_reports(monkeypatch):
    task = ExtractionTaskFactory.create()
    live = ExtractionInstanceFactory.create(task=task, is_processed=False)
    withdrawn = ExtractionInstanceFactory.create(task=task, is_processed=False)
    Report.objects.filter(pk=withdrawn.report.pk).update(withdrawn_at=timezone.now())

    processed: list[int] = []
    monkeypatch.setattr(
        ExtractionTaskProcessor,
        "process_instance",
        lambda self, instance: processed.append(instance.pk),
    )
    ExtractionTaskProcessor(task).process_task(task)

    assert processed == [live.pk]
