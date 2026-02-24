import logging
from itertools import batched

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
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
    assert job.status == ExtractionJob.Status.PENDING

    # Important invariant:
    # While a job is in PREPARING we must not enqueue tasks. Otherwise a worker can pick up a task
    # early and `AnalysisTaskProcessor.start()` will assert because the job is still PREPARING.
    # Tasks may be created while PREPARING, but only enqueued after switching back to PENDING.

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
            query_text=job.query,
            offset=0,
            limit=retrieval_provider.max_results,
            filters=SearchFilters(
                group=job.group.pk,
                language=job.language.code,
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

        # Preparation is complete. Only now do we allow enqueuing tasks.
        job.status = ExtractionJob.Status.PENDING
        job.queued_job_id = None
        job.save()

        tasks_to_enqueue = job.tasks.filter(status=ExtractionTask.Status.PENDING)

    # Ensure the job isn't considered queued anymore once the preparation task has run.
    if job.queued_job_id is not None:
        job.queued_job_id = None
        job.save()

    for task in tasks_to_enqueue:
        if not task.is_queued:
            task.delay()
