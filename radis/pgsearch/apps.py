from django.apps import AppConfig


class PgSearchConfig(AppConfig):
    name = "radis.pgsearch"

    def ready(self):
        from . import signals as signals

        register_app()


def register_app():
    from radis.extractions.site import (
        ExtractionRetrievalProvider,
        register_extraction_retrieval_provider,
    )
    from radis.search.site import SearchProvider, register_search_provider
    from radis.subscriptions.site import (
        SubscriptionFilterProvider,
        SubscriptionRetrievalProvider,
        register_subscription_filter_provider,
        register_subscription_retrieval_provider,
    )

    from .providers import count, filter, retrieve, search

    register_search_provider(
        SearchProvider(
            name="PG Search",
            search=search,
            max_results=1000,
        )
    )

    register_extraction_retrieval_provider(
        ExtractionRetrievalProvider(
            name="PG Search",
            count=count,
            retrieve=retrieve,
            max_results=None,
        )
    )

    register_subscription_retrieval_provider(
        SubscriptionRetrievalProvider(
            name="PG Search",
            retrieve=retrieve,
        )
    )
    register_subscription_filter_provider(
        SubscriptionFilterProvider(
            name="PG Search",
            filter=filter,
        )
    )
