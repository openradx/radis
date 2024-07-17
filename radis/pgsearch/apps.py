from django.apps import AppConfig
from django.conf import settings


class PGSearchConfig(AppConfig):
    name = "radis.pgsearch"

    def ready(self):
        if settings.PGSEARCH_ENABLED:
            register_app()


def register_app():
    from radis.search.site import SearchProvider, register_search_provider

    from .providers import search

    register_search_provider(
        SearchProvider(
            name="PGSearch",
            search=search,
            max_results=1000,
        )
    )
