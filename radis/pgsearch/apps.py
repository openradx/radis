from django.apps import AppConfig
from django.conf import settings
from django.core.checks import Error, register

# Keep in sync with the dimensions= literal in
# radis/pgsearch/migrations/0003_report_embedding.py. The migration
# captures dim at generation time, so changing this requires a new
# migration that re-creates the embedding column.
EMBEDDING_DIM_MIGRATION_LITERAL = 1024


class PgSearchConfig(AppConfig):
    name = "radis.pgsearch"

    def ready(self):
        from . import signals as signals

        register_app()


@register()
def check_embedding_dim_matches_migration(app_configs, **kwargs):
    """Fail loudly when settings.EMBEDDING_DIM diverges from the dim baked
    into migration 0003. Mismatched values would otherwise surface as opaque
    pgvector dimension errors on the first write/query."""
    if settings.EMBEDDING_DIM != EMBEDDING_DIM_MIGRATION_LITERAL:
        return [
            Error(
                f"EMBEDDING_DIM={settings.EMBEDDING_DIM} does not match the dim "
                f"baked into migration 0003 (vector({EMBEDDING_DIM_MIGRATION_LITERAL})). "
                f"Writes will fail with a pgvector dimension error. Either set "
                f"EMBEDDING_DIM={EMBEDDING_DIM_MIGRATION_LITERAL} or write a new "
                f"migration that drops and recreates the embedding column at the new dim.",
                id="pgsearch.E001",
                hint=(
                    "Update EMBEDDING_DIM in your .env, or write a migration that "
                    "matches the new dim and update EMBEDDING_DIM_MIGRATION_LITERAL "
                    "in radis/pgsearch/apps.py."
                ),
            )
        ]
    return []


def register_app():
    from django.conf import settings

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
            max_results=max(
                settings.HYBRID_VECTOR_TOP_K, settings.HYBRID_FTS_MAX_RESULTS
            ),
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
