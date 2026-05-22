from procrastinate.contrib.django import app

from .services import label_reports_in_parallel


@app.task(queue="llm")
def label_report_batch(report_ids: list[int]) -> None:
    label_reports_in_parallel(report_ids)


from .models import LabelingTask
from .processors import LabelingTaskProcessor


@app.task(queue="llm")
def process_labeling_task(task_id: int) -> None:
    task = LabelingTask.objects.get(id=task_id)
    LabelingTaskProcessor(task).start()
    task.queued_job_id = None
    task.save()
