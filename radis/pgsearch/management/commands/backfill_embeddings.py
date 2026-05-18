from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from radis.pgsearch.models import ReportSearchVector
from radis.pgsearch.tasks import enqueue_embed_reports


class Command(BaseCommand):
    help = (
        "Enqueue embed_reports tasks for all reports that don't yet have an "
        "embedding. Idempotent: rows that already have an embedding are skipped."
    )

    def add_arguments(self, parser):
        parser.add_argument("--batch-size", type=int, default=500)
        parser.add_argument(
            "--limit",
            type=int,
            default=None,
            help="Maximum number of reports to enqueue (default: all).",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Print the count of reports that would be enqueued, but enqueue nothing.",
        )

    def handle(self, *args, batch_size, limit, dry_run, **options):
        if batch_size <= 0:
            raise CommandError(f"--batch-size must be > 0, got {batch_size}")
        if limit is not None and limit < 0:
            raise CommandError(f"--limit must be >= 0, got {limit}")

        qs = (
            ReportSearchVector.objects.filter(embedding__isnull=True)
            .order_by("report_id")
            .values_list("report_id", flat=True)
        )
        if limit is not None:
            qs = qs[:limit]

        if dry_run:
            self.stdout.write(f"Dry run: would enqueue {qs.count()} reports.")
            return

        priority = settings.EMBEDDING_BACKFILL_PRIORITY
        total = 0
        chunk: list[int] = []
        # Use a server-side cursor so we don't materialize the whole id set in memory.
        for rid in qs.iterator(chunk_size=batch_size):
            chunk.append(rid)
            if len(chunk) >= batch_size:
                enqueue_embed_reports(chunk, priority=priority)
                total += len(chunk)
                chunk = []
        if chunk:
            enqueue_embed_reports(chunk, priority=priority)
            total += len(chunk)
        self.stdout.write(f"Enqueued {total} reports for embedding.")
