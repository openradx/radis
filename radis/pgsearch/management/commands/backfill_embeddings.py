from django.conf import settings
from django.core.management.base import BaseCommand

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
        qs = (
            ReportSearchVector.objects.filter(embedding__isnull=True)
            .order_by("report_id")
            .values_list("report_id", flat=True)
        )
        if limit is not None:
            qs = qs[:limit]

        ids = list(qs)
        if dry_run:
            self.stdout.write(f"Dry run: would enqueue {len(ids)} reports.")
            return

        priority = settings.EMBEDDING_BACKFILL_PRIORITY
        total = 0
        for start in range(0, len(ids), batch_size):
            chunk = ids[start : start + batch_size]
            enqueue_embed_reports(chunk, priority=priority)
            total += len(chunk)
        self.stdout.write(f"Enqueued {total} reports for embedding.")
