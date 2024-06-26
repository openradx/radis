from typing import Any

from django.apps import AppConfig
from django.conf import settings


class VespaConfig(AppConfig):
    name = "radis.vespa"

    def ready(self):
        if settings.VESPA_ENABLED:
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

    from .providers import count_bm25, retrieve_bm25, search_bm25, search_hybrid, search_semantic
    from .tasks import process_created_reports, process_deleted_reports, process_updated_reports
    from .utils.document_utils import fetch_document
    from .vespa_app import MAX_RETRIEVAL_HITS, MAX_SEARCH_HITS

    def handle_created_reports(reports: list[Report]) -> None:
        process_created_reports.delay([report.id for report in reports])

    register_reports_created_handler(
        ReportsCreatedHandler(
            name="Vespa",
            handle=handle_created_reports,
        )
    )

    def handle_updated_reports(reports: list[Report]) -> None:
        process_updated_reports.delay([report.id for report in reports])

    register_reports_updated_handler(
        ReportsUpdatedHandler(
            name="Vespa",
            handle=handle_updated_reports,
        )
    )

    def handle_deleted_reports(reports: list[Report]) -> None:
        process_deleted_reports.delay([report.document_id for report in reports])

    register_reports_deleted_handler(
        ReportsDeletedHandler(
            name="Vespa",
            handle=handle_deleted_reports,
        )
    )

    def fetch_vespa_document(report: Report) -> dict[str, Any]:
        return fetch_document(report.document_id)

    register_document_fetcher("vespa", fetch_vespa_document)

    register_search_provider(
        SearchProvider(
            name="Vespa Hybrid Ranking",
            search=search_hybrid,
            max_results=MAX_SEARCH_HITS,
        )
    )

    register_search_provider(
        SearchProvider(
            name="Vespa BM25 Ranking",
            search=search_bm25,
            max_results=MAX_SEARCH_HITS,
        )
    )

    register_search_provider(
        SearchProvider(
            name="Vespa Semantic Ranking",
            search=search_semantic,
            max_results=MAX_SEARCH_HITS,
        )
    )

    register_retrieval_provider(
        RetrievalProvider(
            name="Vespa BM25",
            count=count_bm25,
            retrieve=retrieve_bm25,
            max_results=MAX_RETRIEVAL_HITS,
        )
    )
