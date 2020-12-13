from celery import shared_task
from celery.utils.log import get_task_logger
from django.conf import settings
from .models import ContinuousTransferJob

logger = get_task_logger(__name__)


@shared_task(ignore_result=True)
def continuous_transfer(job_id):
    job = ContinuousTransferJob.objects.get(id=job_id)

    priority = settings.CONTINUOUS_TRANSFER_DEFAULT_PRIORITY
    if job.transfer_urgently:
        priority = settings.CONTINUOUS_TRANSFER_URGENT_PRIORITY

    logger.info("Prepare continuous transfer job [Job ID %d].", job_id)


@shared_task
def transfer_next_dicoms(task_id):
    raise NotImplementedError("Transfer continuous task must be implemented.")
