import csv
import logging
import os
import tempfile
from itertools import batched

from django.conf import settings
from django.core.files import File
from django.utils import timezone
from procrastinate.contrib.django import app

from radis.reports.models import Report
from radis.search.site import Search, SearchFilters
from radis.search.utils.query_parser import QueryParser

from .models import (
    ExtractionInstance,
    ExtractionJob,
    ExtractionResultExport,
    ExtractionTask,
)
from .processors import ExtractionTaskProcessor
from .site import extraction_retrieval_providers
from .utils.csv import sanitize_csv_value

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
        provider = job.provider
        retrieval_provider = extraction_retrieval_providers[provider]

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


@app.task
def process_extraction_result_export(export_id: int) -> None:
    export = ExtractionResultExport.objects.select_related("job").get(id=export_id)
    job = export.job

    if export.status not in (
        ExtractionResultExport.Status.PENDING,
        ExtractionResultExport.Status.FAILED,
    ):
        logger.info("Export %s is already being processed or completed", export.pk)
        return

    export.status = ExtractionResultExport.Status.PROCESSING
    export.started_at = timezone.now()
    export.error_message = ""
    export.save(update_fields=["status", "started_at", "error_message"])

    output_fields = list(job.output_fields.order_by("id"))
    instances = (
        ExtractionInstance.objects.filter(task__job=job).order_by("id").only("id", "output")
    )
    chunk_size = getattr(settings, "EXTRACTION_RESULTS_EXPORT_CHUNK_SIZE", 1000)

    temp_file_path = None
    row_count = 0

    try:
        with tempfile.NamedTemporaryFile(
            delete=False, mode="w", encoding="utf-8", newline=""
        ) as tmp:
            temp_file_path = tmp.name
            writer = csv.writer(tmp, lineterminator="\n")

            header = ["id"] + [field.name for field in output_fields]
            writer.writerow(header)

            for instance in instances.iterator(chunk_size=chunk_size):
                output_data = instance.output or {}
                row = [sanitize_csv_value(instance.pk)]
                for field in output_fields:
                    value = output_data.get(field.name)
                    row.append(sanitize_csv_value(value))
                writer.writerow(row)
                row_count += 1

        filename = f"extraction-job-{job.pk}.csv"
        with open(temp_file_path, "rb") as tmp_file:
            export.file.save(filename, File(tmp_file), save=False)

        export.row_count = row_count
        export.status = ExtractionResultExport.Status.COMPLETED
        export.finished_at = timezone.now()
        export.save(update_fields=["file", "row_count", "status", "finished_at"])
    except Exception as err:
        export.status = ExtractionResultExport.Status.FAILED
        export.error_message = str(err)
        export.finished_at = timezone.now()
        export.save(update_fields=["status", "error_message", "finished_at"])
        logger.exception("Failed to generate extraction export %s", export.pk)
        raise
    finally:
        if temp_file_path and os.path.exists(temp_file_path):
            try:
                os.remove(temp_file_path)
            except FileNotFoundError:
                logger.warning(
                    "Temporary export file %s already removed before cleanup",
                    temp_file_path,
                )
            except PermissionError:
                logger.warning(
                    "Insufficient permissions to remove temporary export file %s",
                    temp_file_path,
                )
