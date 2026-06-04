import logging
from concurrent.futures import Future, ThreadPoolExecutor

from django import db
from django.conf import settings

from radis.core.models import AnalysisTask
from radis.core.processors import AnalysisTaskProcessor

from .labeling import label_report
from .models import LabelingTask

logger = logging.getLogger(__name__)


class LabelingTaskProcessor(AnalysisTaskProcessor):
    def process_task(self, task: LabelingTask) -> None:
        had_failure = False
        with ThreadPoolExecutor(max_workers=settings.LABELING_LLM_CONCURRENCY_LIMIT) as executor:
            try:
                futures: list[Future] = []
                for report_id in task.reports.values_list("pk", flat=True):
                    futures.append(executor.submit(self._safe_label, report_id))
                for future in futures:
                    if not future.result():
                        had_failure = True
            finally:
                db.close_old_connections()

        if had_failure:
            task.status = AnalysisTask.Status.WARNING
            task.message = "Some reports failed to label; see logs."

    def _safe_label(self, report_id: int) -> bool:
        try:
            label_report(report_id)
            return True
        except Exception:
            logger.exception("Labeling failed for report %s", report_id)
            return False
        finally:
            db.close_old_connections()
