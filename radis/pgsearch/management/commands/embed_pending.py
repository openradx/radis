"""Enqueue `embed_reports_task` for every `ReportSearchVector` whose embedding
is still NULL.

Operators run this for three scenarios:

1. **Backfill.** Reports loaded before the deferred-embedding wiring shipped.
2. **Dim or model change.** After §4.5: drop the column, re-migrate (or
   `ReportSearchVector.objects.update(embedding=None)` for a same-dim model
   swap), then run this command to re-embed against the new model.
3. **Outage recovery.** Tasks that exhausted Procrastinate retries during an
   extended embedding-service outage — re-run after the service recovers.

The command itself does no HTTP work; it enqueues tasks onto the `embeddings`
queue. The embeddings worker drains them at its configured `--concurrency`,
so operators cannot accidentally hammer the embedding service.

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

from radis.pgsearch.models import ReportSearchVector
from radis.pgsearch.tasks import embed_reports_task


class Command(BaseCommand):
    help = (
        "Enqueue embed_reports_task for every ReportSearchVector with "
        "embedding=NULL. The embeddings worker drains the queue at its "
        "configured concurrency."
    )

    def add_arguments(self, parser) -> None:
        parser.add_argument(
            "--batch-size",
            type=int,
            default=settings.EMBEDDING_BATCH_SIZE,
            help=(
                f"Reports per enqueued task (default "
                f"{settings.EMBEDDING_BATCH_SIZE}). The worker further chunks "
                f"each task by EMBEDDING_BATCH_SIZE internally."
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
            ReportSearchVector.objects.filter(embedding__isnull=True)
            .order_by("report_id")
            .values_list("report_id", flat=True)
        )
        if opts["limit"] is not None:
            ids = ids[: opts["limit"]]
        if not ids:
            self.stdout.write("Nothing to embed.")
            return

        batch_size = opts["batch_size"]
        self.stdout.write(
            f"Enqueuing {len(ids)} report(s) in tasks of {batch_size}..."
        )
        for i in range(0, len(ids), batch_size):
            chunk = ids[i : i + batch_size]
            embed_reports_task.defer(report_ids=list(chunk))
            self.stdout.write(f"  enqueued {i + len(chunk)}/{len(ids)}")
        self.stdout.write(self.style.SUCCESS("Done."))
