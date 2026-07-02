"""Cancel a running embedding backfill.

Counterpart to `embed_pending`: cancels every embed_reports_task subjob
still queued at backfill priority. Subjobs already being executed (at most
the embeddings worker's --concurrency) finish their current chunk; live
write-path embedding subjobs are untouched. Re-running `embed_pending`
later resumes exactly where things stopped — its `embedding IS NULL`
filter makes it idempotent.
"""

import logging

from django.core.management.base import BaseCommand

from radis.pgsearch.tasks import cancel_backfill_embeddings

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = (
        "Cancel every queued embedding-backfill subjob (todo jobs at "
        "backfill priority). Running subjobs finish their current chunk; "
        "live write-path embedding is untouched."
    )

    def handle(self, *args, **opts) -> None:
        cancelled = cancel_backfill_embeddings()
        if cancelled == 0:
            self.stdout.write("No queued backfill subjobs to cancel.")
            return
        self.stdout.write(self.style.SUCCESS(f"Cancelled {cancelled} queued backfill subjob(s)."))
        self.stdout.write(
            "Running subjobs (at most the worker's concurrency) will finish "
            "their current chunk. Re-run `./manage.py embed_pending` to resume."
        )
