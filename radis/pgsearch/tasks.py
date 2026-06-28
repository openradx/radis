import logging

import stamina
from django.conf import settings
from procrastinate.contrib.django import app
from procrastinate.types import JSONValue

from .models import ReportSearchIndex
from .utils.embedding_client import (
    EmbeddingClient,
    EmbeddingClientError,
    EmbeddingPayloadTooLargeError,
)
from .utils.indexing import bulk_upsert_report_search_indexes

logger = logging.getLogger(__name__)


def _is_retryable_embedding_error(exc: Exception) -> bool:
    """stamina retry predicate. Retry transient embedding-service failures
    (5xx, network, timeouts — all surfaced as `EmbeddingClientError`) but
    NOT `EmbeddingPayloadTooLargeError`, which is a deterministic rejection
    of an input that exceeds the model's context window. Retrying that
    one would just hit the same wall — the bisect logic in
    `_embed_with_bisect` handles it instead."""
    return isinstance(exc, EmbeddingClientError) and not isinstance(
        exc, EmbeddingPayloadTooLargeError
    )


@stamina.retry(
    on=_is_retryable_embedding_error,
    attempts=3,
    timeout=30.0,
    wait_initial=0.5,
    wait_max=8.0,
)
def _embed_chunk_with_retry(
    client: EmbeddingClient, texts: list[str]
) -> list[list[float]]:
    """Single embed call wrapped in stamina-controlled transient retries.

    Layered with Procrastinate's task-level retry: stamina handles brief
    blips (3 attempts within ~30s); Procrastinate handles extended outages
    (whole-task retry on backoff). `EmbeddingPayloadTooLargeError` is
    excluded by the predicate so the bisect logic above this layer can
    catch and resolve it without burning retry budget."""
    return client.embed_documents(texts)


@app.task
def bulk_index_reports(report_ids: list[int]) -> None:
    """Deferred FTS bulk-indexing for the bulk-upsert path
    (when `PGSEARCH_SYNC_INDEXING=False`).

    Chains into `embed_reports_task` subjobs once RSV rows exist, so the
    embeddings worker never reads a missing `report.body` or a stale tsvector.
    """
    if not report_ids:
        return
    logger.info("Indexing %s reports in bulk.", len(report_ids))
    bulk_upsert_report_search_indexes(report_ids)
    enqueue_embed_reports(report_ids)


def enqueue_bulk_index_reports(report_ids: list[int]) -> int | None:
    if not report_ids:
        return None
    try:
        payload: list[JSONValue] = [int(report_id) for report_id in report_ids]
    except (TypeError, ValueError) as exc:
        logger.error("Invalid report_id in bulk index request: %s", exc)
        return None
    return app.configure_task(
        "radis.pgsearch.tasks.bulk_index_reports",
        allow_unknown=False,
    ).defer(report_ids=payload)


def enqueue_embed_reports(
    report_ids: list[int],
    *,
    subjob_size: int | None = None,
    priority: int | None = None,
) -> int:
    """Chunk `report_ids` into subjobs and defer one `embed_reports_task`
    per chunk. Returns the number of subjobs deferred.

    Subjob size defaults to `settings.EMBEDDING_SUBJOB_SIZE` (the
    Procrastinate-task granularity). It's distinct from
    `settings.EMBEDDING_BATCH_SIZE` (the per-HTTP-call size inside one
    task). A 1M-report backfill becomes ~10k subjobs of 100, each making
    ~3 HTTP calls of 32 — many workers can drain in parallel, retries
    have bounded blast radius, and a stuck task can't tie up the worker
    on the whole queue's worth of work.

    Priority defaults to `settings.EMBEDDING_LIVE_PRIORITY` (write-path).
    `embed_pending` and the admin backfill action override to
    `settings.EMBEDDING_BACKFILL_PRIORITY`, so a million-row backfill
    can't park itself ahead of every subsequent live ingest write.

    Single call site for every place that enqueues embedding work: the
    write-path handler, the FTS chain tail, `embed_pending`, and the
    admin action. Operators read one knob, not several.
    """
    if not report_ids:
        return 0
    size = subjob_size if subjob_size is not None else settings.EMBEDDING_SUBJOB_SIZE
    if priority is None:
        priority = settings.EMBEDDING_LIVE_PRIORITY
    deferrer = app.configure_task(
        "radis.pgsearch.tasks.embed_reports_task",
        allow_unknown=False,
        priority=priority,
    )
    count = 0
    for start in range(0, len(report_ids), size):
        chunk = report_ids[start : start + size]
        deferrer.defer(report_ids=list(chunk))
        count += 1
    return count


def _embed_with_bisect(
    client: EmbeddingClient,
    rsvs: list[ReportSearchIndex],
    embedded: list[ReportSearchIndex],
    skipped: list[ReportSearchIndex],
) -> None:
    """Embed `rsvs` and append `(rsv, vec)` pairs to `embedded`. When the
    backend rejects the request as too large, bisect and recurse. Once the
    offender is isolated to a single rsv, log its `report_id` + body length
    and append it to `skipped` instead of raising — that way the rest of
    the task's batch still gets embedded.

    Transient errors are absorbed by `_embed_chunk_with_retry`'s stamina
    wrapper. Anything that escapes after stamina's attempts/timeout budget
    is exhausted propagates so Procrastinate's task-level retry applies.
    """
    if not rsvs:
        return
    try:
        vectors = _embed_chunk_with_retry(client, [rsv.report.body for rsv in rsvs])
    except EmbeddingPayloadTooLargeError as exc:
        if len(rsvs) == 1:
            offender = rsvs[0]
            logger.error(
                "embed_reports_task: report_id=%s body_chars=%d rejected by embedding "
                "service as too large; skipping. Backend error: %s",
                offender.report_id,
                len(offender.report.body),
                exc,
            )
            skipped.append(offender)
            return
        mid = len(rsvs) // 2
        _embed_with_bisect(client, rsvs[:mid], embedded, skipped)
        _embed_with_bisect(client, rsvs[mid:], embedded, skipped)
        return

    for rsv, vec in zip(rsvs, vectors, strict=True):
        rsv.embedding = vec
        embedded.append(rsv)


@app.task(queue="embeddings")
def embed_reports_task(report_ids: list[int]) -> None:
    """Embed the named reports.

    Two layers of failure handling sit between the embedding service and
    this task:

    * `_embed_chunk_with_retry` retries transient `EmbeddingClientError`
      via stamina (3 attempts, ~30s budget) for brief blips.
    * `_embed_with_bisect` catches the deterministic
      `EmbeddingPayloadTooLargeError` and recurses until it isolates the
      offending report, then logs ERROR with `report_id` + body length and
      skips it (its RSV stays NULL). The rest of the batch still embeds.

    Anything that escapes both — sustained `EmbeddingClientError` past
    stamina's budget — propagates so Procrastinate's task-level retry
    policy applies.

    Callers must ensure ReportSearchIndex rows exist before deferring this
    task. `bulk_index_reports` chains the defer at the end of its run, and
    `embed_pending` / the admin action filter on existing RSV rows by
    construction.
    """
    if not report_ids:
        return

    rsvs = list(
        ReportSearchIndex.objects.filter(report_id__in=report_ids)
        .select_related("report")
        .only("id", "report_id", "report__body")
    )
    if not rsvs:
        logger.warning(
            "embed_reports_task: no ReportSearchIndex rows for report ids %s",
            report_ids,
        )
        return

    batch_size = settings.EMBEDDING_BATCH_SIZE
    embedded: list[ReportSearchIndex] = []
    skipped: list[ReportSearchIndex] = []
    with EmbeddingClient() as client:
        for start in range(0, len(rsvs), batch_size):
            chunk = rsvs[start : start + batch_size]
            _embed_with_bisect(client, chunk, embedded, skipped)

    if embedded:
        ReportSearchIndex.objects.bulk_update(embedded, fields=["embedding"])
    if skipped:
        logger.error(
            "embed_reports_task: %d report(s) skipped as too large for the embedding "
            "model; report_ids=%s. Fix the upstream report or raise the model context "
            "limit; their RSV rows stay NULL until embedded.",
            len(skipped),
            [rsv.report_id for rsv in skipped],
        )
