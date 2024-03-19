from celery import shared_task

from .site import reports_created_handlers, reports_deleted_handlers, reports_updated_handlers


@shared_task
def reports_created(report_ids: list[int]) -> None:
    for handler in reports_created_handlers:
        handler(report_ids)


@shared_task
def reports_updated(report_ids: list[int]) -> None:
    for handler in reports_updated_handlers:
        handler(report_ids)


@shared_task
def reports_deleted(document_ids: list[str]) -> None:
    for handler in reports_deleted_handlers:
        handler(document_ids)
