from django.core.management.base import BaseCommand, CommandError

from radis.pgsearch.models import EmbeddingBackfillJob
from radis.pgsearch.tasks import enqueue_embedding_backfill
from radis.pgsearch.utils.embedding_client import is_embedding_available


class Command(BaseCommand):
    help = "Generate embeddings for reports that don't have them yet"

    def add_arguments(self, parser):
        parser.add_argument(
            "--force",
            action="store_true",
            help="Regenerate all embeddings, even for reports that already have one",
        )

    def handle(self, *args, **options):
        if not is_embedding_available():
            raise CommandError(
                "No embedding provider configured. Set EXTERNAL_LLM_PROVIDER_URL"
                " to an OpenAI-compatible API that supports embeddings."
            )

        job = EmbeddingBackfillJob.objects.create()
        enqueue_embedding_backfill.defer(backfill_job_id=job.id, force=options["force"])
        self.stdout.write(
            self.style.SUCCESS(f"Embedding backfill job {job.id} created and queued.")
        )
