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
    from radis.vespa.vespa_app import MAX_RETRIEVAL_HITS, MAX_SEARCH_HITS

    from .providers import search_bm25, search_hybrid, search_semantic
    from .utils.document_utils import (
        create_documents,
        delete_documents,
        fetch_document,
        update_documents,
    )

    register_reports_created_handler(lambda report_ids: create_documents(report_ids))

    register_reports_updated_handler(lambda report_ids: update_documents(report_ids))

    register_reports_deleted_handler(lambda document_ids: delete_documents(document_ids))

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
