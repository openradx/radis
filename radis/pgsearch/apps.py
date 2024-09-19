from django.apps import AppConfig


class PgSearchConfig(AppConfig):
    name = "radis.pgsearch"

    def ready(self):
        from . import signals as signals

        register_app()


def register_app():
    from radis.rag.site import RetrievalProvider, register_retrieval_provider
    from radis.search.site import SearchProvider, register_search_provider
    from radis.subscriptions.site import FilterProvider, register_filter_provider

    from .providers import count, filter, retrieve, search

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

    register_filter_provider(
        FilterProvider(
            name="PG Search",
            filter=filter,
            max_results=None,
        )
    )
