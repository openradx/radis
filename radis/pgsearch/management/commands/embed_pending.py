"""Enqueue `embed_reports_task` for every `ReportSearchIndex` whose embedding
is still NULL.

Operators run this for three scenarios:

1. **Backfill.** Reports loaded before the deferred-embedding wiring shipped.
2. **Dim or model change.** After §4.5: drop the column, re-migrate (or
   `ReportSearchIndex.objects.update(embedding=None)` for a same-dim model
   swap), then run this command to re-embed against the new model.
3. **Outage recovery.** Tasks that exhausted Procrastinate retries during an
   extended embedding-service outage — re-run after the service recovers.

The command itself does no HTTP work; it defers Procrastinate tasks onto the
`embeddings` queue. The embeddings worker drains them at its configured
`--concurrency`, so operators cannot accidentally hammer the embedding service.

Chunking goes through the shared `enqueue_embed_reports` helper, so the
subjob size matches what the write-path handler and the admin action use
(default `settings.EMBEDDING_SUBJOB_SIZE`).

Properties:

- **Idempotent.** The filter is `embedding IS NULL`; re-runs are no-ops on
  rows the worker has already drained.
- **Resumable.** No checkpoint state. Killed mid-enqueue → re-run picks up
  the still-NULL rows.
- **Rate-limited.** Worker concurrency caps load on the embedding service
  regardless of how many tasks this command enqueues.
"""
from django.conf import settings
from django.core.management.base import BaseCommand

from radis.pgsearch.models import ReportSearchIndex
from radis.pgsearch.tasks import enqueue_embed_reports


class Command(BaseCommand):
    help = (
        "Enqueue embed_reports_task subjobs for every ReportSearchIndex "
        "with embedding=NULL. The embeddings worker drains the queue at "
        "its configured concurrency."
    )

    def add_arguments(self, parser) -> None:
        parser.add_argument(
            "--subjob-size",
            type=int,
            default=settings.EMBEDDING_SUBJOB_SIZE,
            help=(
                f"Reports per Procrastinate subjob (default "
                f"{settings.EMBEDDING_SUBJOB_SIZE}). The worker further "
                f"chunks each subjob into HTTP calls of "
                f"EMBEDDING_BATCH_SIZE={settings.EMBEDDING_BATCH_SIZE}."
            ),
        )
        parser.add_argument(
            "--limit",
            type=int,
            default=None,
            help="Stop after enqueuing N reports (default: enqueue all).",
        )

    def handle(self, *args, **opts) -> None:
        ids = list(
            ReportSearchIndex.objects.filter(embedding__isnull=True)
            .order_by("report_id")
            .values_list("report_id", flat=True)
        )
        if opts["limit"] is not None:
            ids = ids[: opts["limit"]]
        if not ids:
            self.stdout.write("Nothing to embed.")
            return

        subjob_size = opts["subjob_size"]
        self.stdout.write(
            f"Enqueuing {len(ids)} report(s) in subjobs of {subjob_size}..."
        )
        subjob_count = enqueue_embed_reports(
            ids,
            subjob_size=subjob_size,
            priority=settings.EMBEDDING_BACKFILL_PRIORITY,
        )
        self.stdout.write(
            self.style.SUCCESS(f"Done. Deferred {subjob_count} subjob(s).")
        )
