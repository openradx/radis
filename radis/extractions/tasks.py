import logging
from itertools import batched

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.db import transaction
from procrastinate.contrib.django import app

from radis.reports.models import Report
from radis.search.site import Search, SearchFilters
from radis.search.utils.query_parser import QueryParser

from . import site
from .models import ExtractionInstance, ExtractionJob, ExtractionTask
from .processors import ExtractionTaskProcessor

logger = logging.getLogger(__name__)


@app.task(queue="llm")
def process_extraction_task(task_id: int) -> None:
    task = ExtractionTask.objects.get(id=task_id)
    processor = ExtractionTaskProcessor(task)
    processor.start()

    # The Procrastinate job is finished (success or failure). Clearing queued_job_id makes it
    # possible to re-queue the task later if the user resets/retries it.
    task = ExtractionTask.objects.get(id=task_id)
    task.queued_job_id = None
    task.save()


@app.task
def process_extraction_job(job_id: int) -> None:
    job = ExtractionJob.objects.get(id=job_id)

    logger.info("Start preparing job %s", job)

    # PENDING: a new job, or one re-run while its tasks already exist (resumed below).
    # PREPARING: run again after a worker crashed while preparing it. Anything else
    # has nothing to prepare.
    if job.status not in (ExtractionJob.Status.PENDING, ExtractionJob.Status.PREPARING):
        logger.warning(
            "process_extraction_job called for job %s in status %s, ignoring.",
            job.pk,
            job.get_status_display(),
        )
        return

    # Tasks may be created while the job is PREPARING, but they must not run yet — a worker
    # picking one up early would fail, because the job is not PENDING/IN_PROGRESS. The
    # transaction at the end guarantees this: queue rows appear only once the job is PENDING.

    # A crash during preparation can leave a partial set of tasks behind. They were never
    # enqueued (that only happens once the job is PENDING), so drop them and prepare again.
    if job.status == ExtractionJob.Status.PREPARING:
        job.tasks.all().delete()

    # If tasks already exist, this is a resume/retry path. We keep the job in PENDING and just
    # (re-)enqueue any pending tasks that are currently not queued.
    if job.tasks.exists():
        tasks_to_enqueue = job.tasks.filter(status=ExtractionTask.Status.PENDING)
    else:
        job.status = ExtractionJob.Status.PREPARING
        job.save()

        # This is a newly created job or a job that has been restarted.
        if site.extraction_retrieval_provider is None:
            logger.error("Extraction retrieval provider is not configured for job %s", job)
            raise ImproperlyConfigured("Extraction retrieval provider is not configured.")
        retrieval_provider = site.extraction_retrieval_provider

        logger.debug("Collecting tasks for job %s", job)

        query_node, fixes = QueryParser().parse(job.query)

        if query_node is None:
            raise ValueError(f"Not a valid query (evaluated as empty): {job.query}")

        if len(fixes) > 0:
            logger.info(f"The following fixes were applied to the query:\n{'\n - '.join(fixes)}")

        search = Search(
            query=query_node,
            offset=0,
            limit=retrieval_provider.max_results,
            filters=SearchFilters(
                group=job.group.pk,
                language=job.language.code if job.language else "",
                modalities=list(job.modalities.values_list("code", flat=True)),
                study_date_from=job.study_date_from,
                study_date_till=job.study_date_till,
                study_description=job.study_description,
                patient_sex=job.patient_sex,
                patient_age_from=job.age_from,
                patient_age_till=job.age_till,
            ),
        )

        logger.debug("Searching reports for task with search: %s", search)

        for document_ids in batched(
            retrieval_provider.retrieve(search), settings.EXTRACTION_TASK_BATCH_SIZE
        ):
            logger.debug("Creating an extraction task for document IDs: %s", document_ids)
            task = ExtractionTask.objects.create(job=job, status=ExtractionTask.Status.PENDING)

            for document_id in document_ids:
                report = Report.objects.get(document_id=document_id)
                ExtractionInstance.objects.create(task=task, report_id=report.pk)

        tasks_to_enqueue = job.tasks.filter(status=ExtractionTask.Status.PENDING)

    # Flip to PENDING and enqueue in one transaction: if the worker dies mid-loop, nothing is
    # committed — the job is still PREPARING (or PENDING with no new rows) and the re-run
    # rebuilds or resumes. A half-enqueued PENDING job would strand its remaining tasks.
    with transaction.atomic():
        if job.status == ExtractionJob.Status.PREPARING:
            job.status = ExtractionJob.Status.PENDING
        # The preparation run itself is over; its own queue row must not count as queued.
        job.queued_job_id = None
        job.save()

        for task in tasks_to_enqueue:
            if not task.is_queued:
                task.delay()
