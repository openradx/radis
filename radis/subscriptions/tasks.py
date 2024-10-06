import logging
from datetime import datetime
from itertools import batched

from django.conf import settings
from django.db import transaction
from django.utils import timezone
from pebble import asynchronous
from procrastinate.contrib.django import app

from radis.rag.site import retrieval_providers
from radis.reports.models import Report
from radis.search.site import Search, SearchFilters
from radis.search.utils.query_parser import QueryParser

from .models import Subscription, SubscriptionJob, SubscriptionTask
from .processors import SubscriptionTaskProcessor
from .site import filter_providers

logger = logging.getLogger(__name__)


@app.task(queue="llm")
async def process_subscription_task(task_id: int) -> None:
    @asynchronous.thread
    def _process_subscription_task(task_id: int) -> None:
        task = SubscriptionTask.objects.get(id=task_id)
        processor = SubscriptionTaskProcessor(task)
        processor.start()

    await _process_subscription_task(task_id)

    task = await SubscriptionTask.objects.aget(id=task_id)
    task.queued_job_id = None
    await task.asave()


@app.task
def process_subscription_job(job_id: int) -> None:
    job = SubscriptionJob.objects.get(id=job_id)

    logger.info("Start processing job %s", job)
    assert job.status == SubscriptionJob.Status.PREPARING

    logger.debug("Collecting tasks for job %s", job)

    language_code = ""
    if job.subscription.language and job.subscription.query != "":
        language_code = job.subscription.language.code

    filters = SearchFilters(
        group=job.subscription.group.pk,
        modalities=list(job.subscription.modalities.values_list("code", flat=True)),
        language=language_code,
        study_description=job.subscription.study_description,
        patient_sex=job.subscription.patient_sex,
        patient_age_from=job.subscription.age_from,
        patient_age_till=job.subscription.age_till,
        created_after=job.subscription.last_refreshed,
    )

    if job.subscription.query != "":
        logger.debug("Searching new reports with query and filters for job %s", job)

        provider = job.subscription.provider
        retrieval_provider = retrieval_providers[provider]

        query_node, fixes = QueryParser().parse(job.subscription.query)

        if query_node is None:
            raise ValueError(f"Not a valid query (evaluated as empty): {job.subscription.query}")

        if len(fixes) > 0:
            logger.info(f"The following fixes were applied to the query:\n{"\n - ".join(fixes)}")

        search = Search(
            query=query_node,
            offset=0,
            limit=retrieval_provider.max_results,
            filters=filters,
        )

        new_document_ids = retrieval_provider.retrieve(search)

    else:
        logger.debug("Searching new reports with filters for job %s", job)

        provider = job.subscription.provider
        filter_provider = filter_providers[provider]
        new_document_ids = filter_provider.filter(filters)

    for document_ids in batched(new_document_ids, settings.SUBSCRIPTION_REFRESH_TASK_BATCH_SIZE):
        logger.debug("Creating SubscriptionTask for document IDs: %s", document_ids)
        task = SubscriptionTask.objects.create(job=job, status=SubscriptionTask.Status.PENDING)
        for document_id in document_ids:
            task.reports.add(Report.objects.get(document_id=document_id))

        task.delay()

    logger.debug("Starting SubscriptionTasks done.")

    job.subscription.last_refreshed = timezone.now()
    job.subscription.save()

    job.status = SubscriptionJob.Status.PENDING
    job.queued_job_id = None
    job.save()


@app.periodic(cron=settings.SUBSCRIPTION_CRON)
@app.task()
def subscription_launcher(timestamp: int):
    logger.info("Launching SubscriptionJobs (Timestamp %s)", datetime.fromtimestamp(timestamp))
    subscriptions = Subscription.objects.all()

    for subscription in subscriptions:
        logger.debug(
            "Creating SubscriptionJob for Subscription %s of user %s",
            subscription.name,
            subscription.owner,
        )
        job = SubscriptionJob.objects.create(
            subscription=subscription,
            status=SubscriptionJob.Status.PREPARING,
            owner=subscription.owner,
            owner_id=subscription.owner_id,
            send_finished_mail=subscription.send_finished_mail,
        )
        job.save()
        transaction.on_commit(job.delay)
