from unittest.mock import patch

from radis.labels.tasks import label_report_batch


def test_label_report_batch_calls_parallel_helper():
    with patch("radis.labels.tasks.label_reports_in_parallel") as helper:
        label_report_batch(report_ids=[1, 2, 3])
    helper.assert_called_once_with([1, 2, 3])


from radis.labels.factories import LabelingTaskFactory
from radis.labels.tasks import process_labeling_task


def test_process_labeling_task_invokes_processor():
    task = LabelingTaskFactory()
    with patch("radis.labels.tasks.LabelingTaskProcessor") as ProcessorMock:
        process_labeling_task(task_id=task.id)
    ProcessorMock.assert_called_once()
    ProcessorMock.return_value.start.assert_called_once()
