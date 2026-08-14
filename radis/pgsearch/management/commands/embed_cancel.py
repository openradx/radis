"""Cancel a running embedding backfill.

Counterpart to `embed_pending`: cancels every queued embed_reports_task
subjob belonging to an active backfill run, identified by the run's `run_id`
in the job args (not by priority — `retry_stalled_jobs` can reprioritize a
stalled subjob, so a priority-based cancel would miss it). Subjobs already
being executed (at most the embeddings worker's --concurrency) finish their
current chunk; live write-path embedding subjobs carry no `run_id` and are
untouched. Cancelled jobs are terminal:
re-running `embed_pending` later covers the remaining work by enqueueing
the still-NULL reports as fresh subjobs chunked at the then-current
EMBEDDINGS_SUBJOB_SIZE (its `embedding IS NULL` filter skips everything
already embedded).
"""

import logging

from django.core.management.base import BaseCommand

from radis.pgsearch.tasks import cancel_backfill_embeddings

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = (
        "Cancel every queued embedding-backfill subjob (todo jobs tagged "
        "with an active run's run_id). Running subjobs finish their current "
        "chunk; live write-path embedding is untouched."
    )

    def handle(self, *args, **opts) -> None:
        cancelled = cancel_backfill_embeddings()
        if cancelled == 0:
            self.stdout.write("No queued backfill subjobs to cancel.")
            return
        self.stdout.write(self.style.SUCCESS(f"Cancelled {cancelled} queued backfill subjob(s)."))
        self.stdout.write(
            "Running subjobs (at most the worker's concurrency) will finish "
            "their current chunk. To continue the backfill, re-run "
            "`./manage.py embed_pending` — it enqueues the still-unembedded "
            "reports as fresh subjobs chunked at the current "
            "EMBEDDINGS_SUBJOB_SIZE; cancelled jobs are never revived. "
            "(EMBEDDINGS_BATCH_SIZE and worker concurrency are read at "
            "execution time and don't require re-enqueueing at all.)"
        )
