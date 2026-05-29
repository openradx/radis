from django.apps import AppConfig
from django.conf import settings
from django.core.checks import Error, register


class PgSearchConfig(AppConfig):
    name = "radis.pgsearch"

    def ready(self):
        from . import signals as signals  # noqa: F401

        register_app()


def _migration_embedding_dim() -> int | None:
    """Return the `dimensions` value of `ReportSearchVector.embedding` as
    captured by the on-disk pgsearch migrations. Returns None if the field
    cannot be located (migrations missing or model renamed)."""
    from django.db.migrations.loader import MigrationLoader

    loader = MigrationLoader(connection=None, ignore_no_migrations=True)
    state = loader.project_state()
    try:
        model = state.apps.get_model("pgsearch", "ReportSearchVector")
        return model._meta.get_field("embedding").dimensions
    except (LookupError, AttributeError):
        return None


@register()
def check_embedding_dim_matches_migration(app_configs, **kwargs):
    """Fail loudly when settings.EMBEDDING_DIM diverges from the dim baked
    into the pgsearch migrations. Mismatched values would otherwise surface as
    opaque pgvector dimension errors on the first write or query."""
    migration_dim = _migration_embedding_dim()

    if migration_dim is None:
        return [
            Error(
                "Could not determine the embedding column dimension from the "
                "pgsearch migrations. Either the migrations are missing the "
                "embedding field or the model has been renamed.",
                id="pgsearch.E002",
                hint=(
                    "Verify that `radis/pgsearch/migrations/` contains a "
                    "migration that adds the `embedding` field to "
                    "`ReportSearchVector`, and that `makemigrations pgsearch` "
                    "succeeds without changes."
                ),
            )
        ]

    if settings.EMBEDDING_DIM != migration_dim:
        return [
            Error(
                f"EMBEDDING_DIM={settings.EMBEDDING_DIM} does not match the "
                f"dim baked into the pgsearch migrations "
                f"(vector({migration_dim})). Writes will fail with a pgvector "
                f"dimension error. Either set "
                f"EMBEDDING_DIM={migration_dim}, or run `makemigrations "
                f"pgsearch` to capture the new dim and follow the §4.5 "
                f"procedure to drop and recreate the embedding column.",
                id="pgsearch.E001",
                hint=(
                    "Update EMBEDDING_DIM in your .env to match the existing "
                    "migrations, or generate a new migration that matches the "
                    "new dim."
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
