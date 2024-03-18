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
        ReportEventType,
        register_document_fetcher,
        register_report_handler,
    )
    from radis.search.site import SearchProvider, register_search_provider
    from radis.vespa.providers import retrieve_bm25
    from radis.vespa.vespa_app import MAX_RETRIEVAL_HITS, MAX_SEARCH_HITS

    from .providers import search_bm25, search_hybrid, search_semantic
    from .utils.document_utils import (
        create_document,
        delete_document,
        fetch_document,
        update_document,
    )

    def handle_report(event_type: ReportEventType, document_id: str):
        if event_type in ("created", "updated"):
            report = Report.objects.get(document_id=document_id)
            if event_type == "created":
                create_document(document_id, report)
            elif event_type == "updated":
                update_document(document_id, report)
        elif event_type == "deleted":
            delete_document(document_id)
        else:
            raise ValueError(f"Invalid report event type: {event_type}")

    register_report_handler(handle_report)

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
