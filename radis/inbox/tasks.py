import logging
from datetime import date, datetime

from django.conf import settings
from django.db import transaction
from procrastinate.contrib.django import app

from radis.reports.models import Report
from radis.search.site import Search, SearchFilters

from .models import Inbox, InboxItem, RefreshInboxJob, RefreshInboxTask

logger = logging.getLogger(__name__)


# @app.periodic(cron=settings.INBOX_CRON)
# @app.task()
def refresh_inbox_launcher(timestamp: int):
    logger.info("Launching RefreshInboxJobs (Timestamp %s)", datetime.fromtimestamp(timestamp))
    inboxes = Inbox.objects.all()

    for inbox in inboxes:
        logger.debug("Creating RefreshInboxJob for Inbox %s of user %s", inbox.name, inbox.owner)
        job = RefreshInboxJob.objects.create(
            inbox=inbox,
            status=RefreshInboxJob.Status.PREPARING,
            owner_id=inbox.owner_id,
            owner=inbox.owner,
            group=inbox.group,
        )
        job.save()
        transaction.on_commit(job.delay)


@app.task()
def process_refresh_inbox_job(job_id: int) -> None:
    job = RagJob.objects.get(id=job_id)

    logger.info("Start processing job %s", job)
    assert job.status == RagJob.Status.PREPARING

    provider = job.provider
    retrieval_provider = retrieval_providers[provider]

    logger.debug("Collecting tasks for job %s", job)

    query_node, fixes = QueryParser().parse(job.query)

    if query_node is None:
        raise ValueError(f"Not a valid query (evaluated as empty): {job.query}")

    if len(fixes) > 0:
        logger.info(f"The following fixes were applied to the query:\n{"\n - ".join(fixes)}")

    search = Search(
        query=query_node,
        offset=0,
        limit=retrieval_provider.max_results,
        filters=SearchFilters(
            group=job.group.pk,
            language=job.language.inbox.code,
            modalities=list(job.inbox.modalities.values_list("code", flat=True)),
            study_date_from=job.inbox.study_date_from,
            study_date_till=job.inbox.study_date_till,
            study_description=job.inbox.study_description,
            patient_sex=job.inbox.patient_sex,  # type: ignore
            patient_age_from=job.inbox.age_from,
            patient_age_till=job.inbox.age_till,
            created_after=job.inbox.last_refreshed,
        ),
    )

    logger.debug("Searching reports for task with search: %s", search)


@app.task()
def process_refresh_inbox_task(task_id: int) -> None:
    task = RefreshInboxTask.objects.get(id=task_id)
    report = Report.objects.get(id=document_id)
    inbox_item = InboxItem(
        inbox=task.job.inbox,
        report=report,
    )
    inbox_item.save()
