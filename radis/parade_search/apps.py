from django.apps import AppConfig


class ParadeSearchConfig(AppConfig):
    name = "radis.parade_search"

    def ready(self):
        from . import signals as signals

        register_app()


def register_app():
    from radis.rag.site import RetrievalProvider, register_retrieval_provider
    from radis.search.site import SearchProvider, register_search_provider

    from .providers import count, retrieve, search

    register_search_provider(
        SearchProvider(
            name="ParadeDB Search",
            search=search,
            max_results=1000,
        )
    )

    register_retrieval_provider(
        RetrievalProvider(
            name="ParadeDB Search",
            count=count,
            retrieve=retrieve,
            max_results=None,
        )
    )
