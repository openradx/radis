from typing import Any

from django.apps import AppConfig


class VespaConfig(AppConfig):
    name = "radis.vespa"

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
    from radis.vespa.providers import retrieve_bm25
    from radis.vespa.tasks import process_deleted_reports, process_updated_reports
    from radis.vespa.vespa_app import MAX_RETRIEVAL_HITS, MAX_SEARCH_HITS

    from .providers import search_bm25, search_hybrid, search_semantic
    from .tasks import process_created_reports
    from .utils.document_utils import fetch_document

    def handle_created_reports(report_ids: list[int]) -> None:
        process_created_reports.delay(report_ids)

    register_reports_created_handler(handle_created_reports)

    def handle_updated_reports(report_ids: list[int]) -> None:
        process_updated_reports.delay(report_ids)

    register_reports_updated_handler(handle_updated_reports)

    def handle_deleted_reports(document_ids: list[str]) -> None:
        process_deleted_reports.delay(document_ids)

    register_reports_deleted_handler(handle_deleted_reports)

    def fetch_vespa_document(report: Report) -> dict[str, Any]:
        return fetch_document(report.document_id)

    register_document_fetcher("vespa", fetch_vespa_document)

    register_search_provider(
        SearchProvider(
            name="Vespa Hybrid Ranking",
            handler=search_hybrid,
            max_results=MAX_SEARCH_HITS,
            info_template="vespa/_hybrid_info.html",
        )
    )

    register_search_provider(
        SearchProvider(
            name="Vespa BM25 Ranking",
            handler=search_bm25,
            max_results=MAX_SEARCH_HITS,
            info_template="vespa/_bm25_info.html",
        )
    )

    register_search_provider(
        SearchProvider(
            name="Vespa Semantic Ranking",
            handler=search_semantic,
            max_results=MAX_SEARCH_HITS,
            info_template="vespa/_bm25_info.html",
        )
    )

    register_retrieval_provider(
        RetrievalProvider(
            name="Vespa BM25",
            handler=retrieve_bm25,
            max_results=MAX_RETRIEVAL_HITS,
            info_template="vespa/_bm25_info.html",
        )
    )
