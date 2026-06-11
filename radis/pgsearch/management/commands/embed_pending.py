"""Embed every `ReportSearchVector` row whose `embedding` is still NULL.

Uses the exact same async path the ADRF report views use
(`embed_reports_inline` → `AsyncEmbeddingClient`) so there is one embedding
code path in the system, not two. Operators run this for three scenarios:

1. **Backfill.** Reports loaded before the inline-embedding wiring shipped.
2. **Dim or model change.** After §4.5: drop the column, re-migrate (or
   `ReportSearchVector.objects.update(embedding=None)` for a same-dim model
   swap), then run this command to re-embed against the new model.
3. **Outage recovery.** If the embedding service was unreachable during a
   window of ADRF writes, those reports were saved with `embedding=NULL`.
   Re-run this command after the service recovers.

Properties:

- **Idempotent.** The filter is `embedding IS NULL`; re-runs only pick up
  remaining work.
- **Resumable.** No checkpoint state. Killed mid-run → next run picks up
  exactly the still-NULL rows.
- **Race-tolerant.** Safe to run alongside live API traffic. If a concurrent
  ADRF view embeds the same row, both produce identical vectors (the model
  is deterministic given the same body), so the second write is a harmless
  re-write of the same value. Cost: one extra embedding HTTP call per
  overlapping row. For best efficiency, run during quiet periods.
"""
import asyncio

from django.conf import settings
from django.core.management.base import BaseCommand

from radis.pgsearch.models import ReportSearchVector
from radis.pgsearch.utils.inline_embedding import embed_reports_inline


class Command(BaseCommand):
    help = (
        "Embed every ReportSearchVector with embedding=NULL using the same "
        "async path as the ADRF API views. Idempotent and resumable. Safe "
        "to run alongside live traffic (overlapping rows are embedded twice; "
        "the result is identical so this only costs one extra HTTP call)."
    )

    def add_arguments(self, parser) -> None:
        parser.add_argument(
            "--batch-size",
            type=int,
            default=settings.EMBEDDING_BATCH_SIZE,
            help=(
                f"Reports per embedding HTTP call "
                f"(default {settings.EMBEDDING_BATCH_SIZE})."
            ),
        )
        parser.add_argument(
            "--limit",
            type=int,
            default=None,
            help="Stop after N reports (default: drain all).",
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

        self.stdout.write(
            f"Embedding {len(ids)} report(s) in batches of {opts['batch_size']}..."
        )
        asyncio.run(self._drain(ids, opts["batch_size"]))
        self.stdout.write(self.style.SUCCESS("Done."))

    async def _drain(self, ids: list[int], batch_size: int) -> None:
        for i in range(0, len(ids), batch_size):
            chunk = ids[i : i + batch_size]
            await embed_reports_inline(chunk)
            self.stdout.write(f"  {i + len(chunk)}/{len(ids)}")
