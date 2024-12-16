import logging
from itertools import batched

from django.conf import settings
from pebble import asynchronous
from procrastinate.contrib.django import app

from radis.reports.models import Report
from radis.search.site import Search, SearchFilters
from radis.search.utils.query_parser import QueryParser

from .models import RagInstance, RagJob, RagTask
from .processors import RagTaskProcessor
from .site import retrieval_providers

logger = logging.getLogger(__name__)


@app.task(queue="llm")
async def process_rag_task(task_id: int) -> None:
    # We have to run RagTaskProcessor in a separate thread because it is
    # creating an async loop itself.
    @asynchronous.thread
    def _process_tag_task(task_id: int) -> None:
        task = RagTask.objects.get(id=task_id)
        processor = RagTaskProcessor(task)
        processor.start()

    await _process_tag_task(task_id) # type: ignore


@app.task
def process_rag_job(job_id: int) -> None:
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

    for document_ids in batched(retrieval_provider.retrieve(search), settings.RAG_TASK_BATCH_SIZE):
        logger.debug("Creating RAG task for document IDs: %s", document_ids)
        task = RagTask.objects.create(job=job, status=RagTask.Status.PENDING)

        for document_id in document_ids:
            report = Report.objects.get(document_id=document_id)
            RagInstance.objects.create(task=task, report_id=report.pk)

        task.delay()

    job.status = RagJob.Status.PENDING
    job.save()
