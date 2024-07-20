from django.apps import AppConfig


class PgSearchConfig(AppConfig):
    name = "radis.pgsearch"

    def ready(self):
        from . import signals as signals

        register_app()


def register_app():
    from radis.rag.site import RetrievalProvider, register_retrieval_provider
    from radis.search.site import SearchProvider, register_search_provider

    from .providers import count, retrieve, search

    register_search_provider(
        SearchProvider(
            name="PG Search",
            search=search,
            max_results=1000,
        )
    )

    register_retrieval_provider(
        RetrievalProvider(
            name="PG Search",
            count=count,
            retrieve=retrieve,
            max_results=None,
        )
    )
