import logging
from itertools import batched

from django.conf import settings
from procrastinate.contrib.django import app

from radis.reports.models import Report
from radis.search.site import Search, SearchFilters
from radis.search.utils.query_parser import QueryParser

from .models import ExtractionInstance, ExtractionJob, ExtractionTask
from .processors import ExtractionTaskProcessor
from .site import extraction_retrieval_provider

logger = logging.getLogger(__name__)


@app.task(queue="llm")
def process_extraction_task(task_id: int) -> None:
    task = ExtractionTask.objects.get(id=task_id)
    processor = ExtractionTaskProcessor(task)
    processor.start()


@app.task
def process_extraction_job(job_id: int) -> None:
    job = ExtractionJob.objects.get(id=job_id)

    logger.info("Start preparing job %s", job)
    assert job.status == ExtractionJob.Status.PENDING

    job.status = ExtractionJob.Status.PREPARING
    job.save()

    if job.tasks.exists():
        # The job has already attached tasks from a previous run.
        # So the job should be resumed or retried.
        tasks = job.tasks.filter(status=ExtractionTask.Status.PENDING)
        for task in tasks:
            if not task.is_queued:
                task.delay()
    else:
        # This is a newly created job or a job that has been restarted.
        assert extraction_retrieval_provider is not None
        retrieval_provider = extraction_retrieval_provider

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

            task.delay()

        job.status = ExtractionJob.Status.PENDING
        job.save()
