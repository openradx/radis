from typing import Any

from django.apps import AppConfig


class OpenSearchConfig(AppConfig):
    name = "radis.opensearch"

    def ready(self):
        register_app()


def register_app():
    from radis.rag.site import RetrievalProvider, register_retrieval_provider
    from radis.reports.models import Report
    from radis.reports.site import (
        register_document_fetcher,
        register_reports_created_handler,
        register_reports_deleted_handler,
        register_reports_updated_handler,
    )
    from radis.search.site import SearchProvider, register_search_provider

    from .providers import count, retrieve, search
    from .tasks import process_created_reports, process_deleted_reports, process_updated_reports
    from .utils.document_utils import (
        fetch_document,
    )

    def handle_created_reports(report_ids: list[int]) -> None:
        process_created_reports.delay(report_ids)

    register_reports_created_handler(handle_created_reports)

    def handle_updated_reports(report_ids: list[int]) -> None:
        process_updated_reports.delay(report_ids)

    register_reports_updated_handler(handle_updated_reports)

    def handle_deleted_reports(document_ids: list[str]) -> None:
        process_deleted_reports.delay(document_ids)

    register_reports_deleted_handler(handle_deleted_reports)

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
