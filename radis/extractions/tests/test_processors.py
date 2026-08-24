from unittest.mock import patch

import pytest
from adit_radis_shared.accounts.factories import UserFactory

from radis.core.models import AnalysisJob, AnalysisTask
from radis.extractions.factories import (
    ExtractionInstanceFactory,
    ExtractionJobFactory,
    ExtractionTaskFactory,
)
from radis.extractions.processors import ExtractionTaskProcessor


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
