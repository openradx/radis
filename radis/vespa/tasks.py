from celery import shared_task

from radis.vespa.utils.document_utils import create_documents, delete_documents, update_documents


@shared_task(queue="vespa_queue")
def process_created_reports(report_ids: list[int]) -> None:
    create_documents(report_ids)


@shared_task(queue="vespa_queue")
def process_updated_reports(report_ids: list[int]) -> None:
    update_documents(report_ids)


@shared_task(queue="vespa_queue")
def process_deleted_reports(document_ids: list[str]) -> None:
    delete_documents(document_ids)
