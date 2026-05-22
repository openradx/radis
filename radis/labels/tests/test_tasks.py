from unittest.mock import patch

from radis.labels.tasks import label_report_batch


def test_label_report_batch_calls_parallel_helper():
    with patch("radis.labels.tasks.label_reports_in_parallel") as helper:
        label_report_batch(report_ids=[1, 2, 3])
    helper.assert_called_once_with([1, 2, 3])
