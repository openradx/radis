import logging
from concurrent.futures import Future, ThreadPoolExecutor

from django import db
from django.conf import settings

from radis.core.models import AnalysisTask
from radis.core.processors import AnalysisTaskProcessor

from .labeling import label_report
from .models import LabelingTask

logger = logging.getLogger(__name__)

# Cap how many per-report failure lines are written to task.log so a fully-failing
# batch (up to LABELING_TASK_BATCH_SIZE reports) cannot bloat the row.
_MAX_LOGGED_FAILURES = 200


class LabelingTaskProcessor(AnalysisTaskProcessor):
    def process_task(self, task: LabelingTask) -> None:
        total = 0
        failures: list[tuple[int, str]] = []
        with ThreadPoolExecutor(max_workers=settings.LABELING_LLM_CONCURRENCY_LIMIT) as executor:
            try:
                futures: list[Future] = []
                for report_id in task.reports.values_list("pk", flat=True):
                    total += 1
                    futures.append(executor.submit(self._safe_label, report_id))
                for future in futures:
                    failure = future.result()
                    if failure is not None:
                        failures.append(failure)
            finally:
                db.close_old_connections()

        if failures:
            task.status = AnalysisTask.Status.WARNING
            task.message = f"{len(failures)} of {total} reports failed to label."
            task.log = self._format_failure_log(failures)

    @staticmethod
    def _format_failure_log(failures: list[tuple[int, str]]) -> str:
        lines = [
            f"Report {report_id}: {error}" for report_id, error in failures[:_MAX_LOGGED_FAILURES]
        ]
        remaining = len(failures) - _MAX_LOGGED_FAILURES
        if remaining > 0:
            lines.append(f"… and {remaining} more")
        return "\n".join(lines)

    def _safe_label(self, report_id: int) -> tuple[int, str] | None:
        try:
            label_report(report_id)
            return None
        except Exception as err:
            logger.exception("Labeling failed for report %s", report_id)
            return report_id, f"{type(err).__name__}: {err}"
        finally:
            db.close_old_connections()
