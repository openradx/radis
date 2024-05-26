from typing import Any

from django.apps import AppConfig
from django.conf import settings


class OpenSearchConfig(AppConfig):
    name = "radis.opensearch"

    def ready(self):
        if settings.OPENSEARCH_ENABLED:
            register_app()


def register_app():
    from radis.rag.site import RetrievalProvider, register_retrieval_provider
    from radis.reports.models import Report
    from radis.reports.site import (
        ReportsCreatedHandler,
        ReportsDeletedHandler,
        ReportsUpdatedHandler,
        register_document_fetcher,
        register_reports_created_handler,
        register_reports_deleted_handler,
        register_reports_updated_handler,
    )
    from radis.search.site import SearchProvider, register_search_provider

    from .providers import count, retrieve, search
    from .utils.document_utils import (
        create_documents,
        delete_documents,
        fetch_document,
        update_documents,
    )

    def handle_created_reports(report_ids: list[int]) -> None:
        create_documents(report_ids)

    register_reports_created_handler(
        ReportsCreatedHandler(
            name="OpenSearch",
            handle=handle_created_reports,
        )
    )

    def handle_updated_reports(report_ids: list[int]) -> None:
        update_documents(report_ids)

    register_reports_updated_handler(
        ReportsUpdatedHandler(
            name="OpenSearch",
            handle=handle_updated_reports,
        )
    )

    def handle_deleted_reports(document_ids: list[str]) -> None:
        delete_documents(document_ids)

    register_reports_deleted_handler(
        ReportsDeletedHandler(
            name="OpenSearch",
            handle=handle_deleted_reports,
        )
    )

    def fetch_opensearch_document(report: Report) -> dict[str, Any]:
        return fetch_document(report.document_id)

    register_document_fetcher("opensearch", fetch_opensearch_document)

    register_search_provider(
        SearchProvider(
            name="OpenSearch BM25",
            search=search,
            max_results=1000,
            info_template="",  # TODO:
        )
    )

    register_retrieval_provider(
        RetrievalProvider(
            name="OpenSearch BM25",
            count=count,
            retrieve=retrieve,
            max_results=None,
            info_template="",  # TODO:
        )
    )
