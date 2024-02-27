from celery import shared_task

from .site import report_event_handlers


@shared_task
def report_created(document_id: str) -> None:
    for handler in report_event_handlers:
        handler("created", document_id)


@shared_task
def report_updated(document_id: str) -> None:
    for handler in report_event_handlers:
        handler("updated", document_id)


@shared_task
def report_deleted(document_id: str) -> None:
    for handler in report_event_handlers:
        handler("deleted", document_id)
