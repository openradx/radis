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
    from radis.search.site import Search, register_search_handler
    from radis.vespa.utils.vespa_utils import (
        create_document,
        delete_document,
        fetch_document,
        search_bm25,
        update_document,
    )

    from .utils.vespa_utils import dictify_report_for_vespa

    def handle_report(event_type: ReportEventType, report: Report):
        # Sync reports with Vespa
        if event_type == "created":
            create_document(report.document_id, dictify_report_for_vespa(report))
        elif event_type == "updated":
            update_document(report.document_id, dictify_report_for_vespa(report))
        elif event_type == "deleted":
            delete_document(report.document_id)

    register_report_handler(handle_report)

    def fetch_vespa_document(report: Report) -> dict[str, Any]:
        return fetch_document(report.document_id)

    register_document_fetcher("vespa", fetch_vespa_document)

    def search_vespa_bm25(search: Search) -> SearchResult:
        return search_bm25(search.query, search.offset, search.page_size)

    register_search_handler("Vespa BM25", search_vespa_bm25, "vespa/_bm25_help.html")
