from procrastinate.contrib.django import app

from .services import label_reports_in_parallel


@app.task(queue="llm")
def label_report_batch(report_ids: list[int]) -> None:
    label_reports_in_parallel(report_ids)
