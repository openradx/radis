import logging
import os

from django.apps import AppConfig
from django.conf import settings
from django.core.checks import Error, register

logger = logging.getLogger(__name__)


class PgSearchConfig(AppConfig):
    name = "radis.pgsearch"

    def ready(self):
        import stamina.instrumentation

        from . import signals as signals  # noqa: F401
        from .tasks import _log_stamina_retry

        stamina.instrumentation.set_on_retry_hooks([_log_stamina_retry])

        register_app()


def _migration_embedding_dim() -> int | None:
    """Return the `dimensions` value of `ReportSearchIndex.embedding` as
    captured by the on-disk pgsearch migrations. Returns None if the field
    cannot be located (migrations missing or model renamed)."""
    from django.db.migrations.loader import MigrationLoader

    loader = MigrationLoader(connection=None, ignore_no_migrations=True)
    state = loader.project_state()
    try:
        model = state.apps.get_model("pgsearch", "ReportSearchIndex")
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
                    "`ReportSearchIndex`, and that `makemigrations pgsearch` "
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


@register()
def check_legacy_embedding_vars(app_configs, **kwargs):
    """Fail startup if the deployment still carries env vars removed in the
    OpenAI-SDK migration (see docs/superpowers/specs/2026-06-30-embedding-
    client-openai-sdk-design.md). A silent ignore would let a misconfigured
    `.env` produce subtly wrong embedding-service URLs."""
    errors = []
    if os.environ.get("EMBEDDING_BACKEND"):
        errors.append(
            Error(
                "EMBEDDING_BACKEND is no longer supported; remove it from .env. "
                "All embedding providers now use the OpenAI-compatible "
                "/v1/embeddings endpoint via the openai SDK.",
                id="pgsearch.E002",
            )
        )
    if os.environ.get("EMBEDDING_PROVIDER_PATH"):
        errors.append(
            Error(
                "EMBEDDING_PROVIDER_PATH is no longer supported; append the path "
                "to EMBEDDING_PROVIDER_URL instead "
                "(e.g. http://host:11434/v1).",
                id="pgsearch.E003",
            )
        )
    return errors


def _index_reports(reports):
    """pgsearch's subscriber on reports_created_handlers / reports_updated_handlers.

    Owns both FTS indexing and embedding for the touched reports. The mode
    flag `PGSEARCH_SYNC_INDEXING` controls whether FTS runs inline on the
    request thread or is deferred to a Procrastinate task on the `default`
    queue. Embedding is always deferred to the `embeddings` queue.

    Ordering between FTS and embedding is the same in both modes: RSI rows
    exist (and `report.body` is reachable) before `embed_reports_task` runs.
    In sync mode the handler upserts inline, then defers embed. In async
    mode the handler only enqueues `bulk_index_reports`; that task chains
    `embed_reports_task` at the end of its own run, so the embeddings worker
    never picks up a report before its RSI row is committed.
    """
    if not reports:
        return

    logger.info(
        "pgsearch.index_reports: handler invoked; reports=%d mode=%s",
        len(reports),
        "sync" if settings.PGSEARCH_SYNC_INDEXING else "async",
    )

    from radis.pgsearch.tasks import enqueue_bulk_index_reports, enqueue_embed_reports
    from radis.pgsearch.utils.indexing import bulk_upsert_report_search_indexes

    report_ids = [report.pk for report in reports]
    if settings.PGSEARCH_SYNC_INDEXING:
        bulk_upsert_report_search_indexes(report_ids)
        enqueue_embed_reports(report_ids)
    else:
        enqueue_bulk_index_reports(report_ids)


def register_app():
    from django.conf import settings

    from radis.extractions.site import (
        ExtractionRetrievalProvider,
        register_extraction_retrieval_provider,
    )
    from radis.reports.site import (
        ReportsCreatedHandler,
        ReportsUpdatedHandler,
        register_reports_created_handler,
        register_reports_updated_handler,
    )
    from radis.search.site import SearchProvider, register_search_provider
    from radis.subscriptions.site import (
        SubscriptionFilterProvider,
        SubscriptionRetrievalProvider,
        register_subscription_filter_provider,
        register_subscription_retrieval_provider,
    )

    from .providers import count, filter, retrieve, search

    register_reports_created_handler(ReportsCreatedHandler(name="PG Search", handle=_index_reports))
    register_reports_updated_handler(ReportsUpdatedHandler(name="PG Search", handle=_index_reports))

    register_search_provider(
        SearchProvider(
            name="PG Search",
            search=search,
            max_results=max(settings.HYBRID_VECTOR_TOP_K, settings.HYBRID_FTS_MAX_RESULTS),
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
