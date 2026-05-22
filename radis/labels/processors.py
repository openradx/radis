import logging

from radis.core.models import AnalysisTask
from radis.core.processors import AnalysisTaskProcessor

from .models import LabelingTask
from .services import label_reports_in_parallel

logger = logging.getLogger("radis.labels")


class LabelingTaskProcessor(AnalysisTaskProcessor):
    def process_task(self, task: LabelingTask) -> None:
        report_ids = list(task.reports.values_list("id", flat=True))
        success, failure = label_reports_in_parallel(report_ids)

        if failure == 0:
            task.status = AnalysisTask.Status.SUCCESS
            task.message = ""
        elif success == 0:
            task.status = AnalysisTask.Status.FAILURE
            task.message = f"All {failure} report labelings failed."
        else:
            task.status = AnalysisTask.Status.WARNING
            task.message = f"{failure} of {success + failure} report labelings failed."
