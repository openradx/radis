import logging
from datetime import time, timedelta
from threading import Event

import redis
from django.conf import settings
from django.db.models import Q
from django.utils import timezone

from .models import QueuedTask
from .site import task_processors
from .utils.db_utils import ensure_db_connection
from .utils.worker_utils import in_time_slot

logger = logging.getLogger(__name__)

TimeSlot = tuple[time, time]

DISTRIBUTED_LOCK = "task_worker_lock"
MAX_PRIORITY = 10


class TaskWorker:
    def __init__(
        self,
        polling_interval: float = 5,
        monitor_interval: float = 5,
        time_slot: TimeSlot | None = None,
    ) -> None:
        self._polling_interval = polling_interval
        self._monitor_interval = monitor_interval
        self._time_slot = time_slot

        self._redis = redis.Redis.from_url(settings.REDIS_URL)
        self._stop_worker = Event()

    def run(self) -> None:
        while True:
            if self._stop_worker.is_set():
                break

            if self._time_slot and not in_time_slot(
                self._time_slot[0], self._time_slot[1], timezone.now().time()
            ):
                self._stop_worker.wait(self._polling_interval)
                continue

            if not self.check_and_process_next_task():
                self._stop_worker.wait(self._polling_interval)
                continue

    def check_and_process_next_task(self) -> bool:
        """Check for a queued task and process it if found.

        Returns: True if a task was processed, False otherwise
        """
        queued_task = self._fetch_queued_task()
        if not queued_task:
            return False

        processor_name = queued_task.processor_name
        Processor = task_processors.get(processor_name)
        if not Processor:
            raise ValueError(f"Unknown report processor with name {processor_name}.")
        processor = Processor()

        if processor.is_suspended():
            delta = timedelta(minutes=60)
            queued_task.eta = timezone.now() + delta
            queued_task.save()
            logger.info(
                f"Processor with name{processor_name} is suspended. "
                "Rescheduling in {humanize.naturaldelta(delta)}."
            )
            return False

        # We have to lock this queued task so that no other worker can pick it up
        queued_task.locked = True
        queued_task.save()

        obj = queued_task.content_object
        logger.info(f"Processing {obj}.")

        try:
            processor.process(obj)
            logger.info(f"Processing of {obj} finished.")
        except Exception:
            logger.exception("Unexpected error while processing %s.", obj)
        finally:
            ensure_db_connection()
            queued_task.delete()

        return True

    def _fetch_queued_task(self) -> QueuedTask | None:
        with self._redis.lock(DISTRIBUTED_LOCK):
            queued_tasks = QueuedTask.objects.filter(locked=False)
            queued_tasks = queued_tasks.filter(Q(eta=None) | Q(eta__lt=timezone.now()))
            queued_tasks = queued_tasks.order_by("-priority", "created")
            queued_task = queued_tasks.first()

            if not queued_task:
                return None

        logger.debug(f"Next queued task being processed: {queued_task}")
        return queued_task

    def shutdown(self) -> None:
        logger.info("Shutting down DICOM worker...")

        with self._redis.lock(DISTRIBUTED_LOCK):
            self._stop_worker.set()

        self._redis.close()
