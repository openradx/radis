from typing import Any

from django.apps import AppConfig


class VespaConfig(AppConfig):
    name = "radis.vespa"

    def ready(self):
        register_app()


def register_app():
    from radis.reports.models import Report
    from radis.reports.site import (
        ReportEventType,
        register_document_fetcher,
        register_report_handler,
    )
    from radis.search.models import SearchResult
    from radis.search.site import Search, register_search_provider

    from .utils.document_utils import (
        create_document,
        delete_document,
        fetch_document,
        update_document,
    )
    from .utils.search_methods import search_bm25, search_hybrid

    def handle_report(event_type: ReportEventType, report: Report):
        # Sync reports with Vespa
        if event_type == "created":
            create_document(report.document_id, report)
        elif event_type == "updated":
            update_document(report.document_id, report)
        elif event_type == "deleted":
            delete_document(report.document_id)

    register_report_handler(handle_report)

    def fetch_vespa_document(report: Report) -> dict[str, Any]:
        return fetch_document(report.document_id)

    register_document_fetcher("vespa", fetch_vespa_document)

    def search_vespa_bm25(search: Search) -> SearchResult:
        return search_bm25(search.query, search.offset, search.page_size)

    register_search_provider("Vespa BM25", search_vespa_bm25, "vespa/_bm25_info.html")

    def search_vespa_hybrid(search: Search) -> SearchResult:
        return search_hybrid(search.query, search.offset, search.page_size)

    register_search_provider("Vespa Hybrid", search_vespa_hybrid, "vespa/_hybrid_info.html")
