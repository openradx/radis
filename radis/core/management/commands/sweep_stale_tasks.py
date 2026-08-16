import logging

from django.core.management.base import BaseCommand

from radis.core.utils.recovery import sweep_stale_analysis_state

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = "Repair analysis tasks left IN_PROGRESS by a killed worker."

    def handle(self, *args, **options):
        self.stdout.write("Sweeping stale analysis tasks... ", ending="")
        self.stdout.flush()

        # This runs before bg_worker in the container start command (chained with &&),
        # so it must never fail: the worker must start even if the sweep breaks.
        try:
            sweep_stale_analysis_state()
        except Exception:
            logger.exception("Sweeping stale analysis tasks failed.")
            self.stdout.write("failed (see logs)")
        else:
            self.stdout.write("done")
