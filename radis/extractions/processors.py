import logging
from concurrent.futures import Future, ThreadPoolExecutor
from string import Template

from django import db
from django.conf import settings

from radis.core.processors import AnalysisTaskProcessor
from radis.core.utils.llm_client import LLMClient
from radis.extractions.utils.processor_utils import (
    generate_output_fields_prompt,
    generate_output_fields_schema,
)

from .models import ExtractionInstance, ExtractionTask

logger = logging.getLogger(__name__)


class ExtractionTaskProcessor(AnalysisTaskProcessor):
    def __init__(self, task: ExtractionTask) -> None:
        super().__init__(task)
        self.client = LLMClient()

    def process_task(self, task: ExtractionTask) -> None:
        with ThreadPoolExecutor(max_workers=settings.EXTRACTION_LLM_CONCURRENCY_LIMIT) as executor:
            try:
                futures: list[Future] = []
                # Skip instances an earlier run of this task (killed mid-way) already processed.
                for instance in task.instances.filter(is_processed=False):
                    future = executor.submit(self.process_instance, instance)
                    futures.append(future)

                exceptions: list[Exception] = []
                for future in futures:
                    try:
                        future.result()
                    except Exception as e:
                        logger.error("Error processing instance in extraction task: %s", e)
                        exceptions.append(e)

                if exceptions:
                    raise exceptions[0]

            finally:
                db.close_old_connections()

    def process_instance(self, instance: ExtractionInstance) -> None:
        # Runs in a worker thread; its connection must be closed in that same
        # thread, also when processing fails.
        try:
            assert not instance.is_processed
            instance.text = instance.report.body
            self.process_output_fields(instance)
            instance.is_processed = True
            instance.save()
        finally:
            db.connections.close_all()

    def process_output_fields(self, instance: ExtractionInstance) -> None:
        job = instance.task.job
        output_fields = list(job.output_fields.order_by("pk"))
        Schema = generate_output_fields_schema(output_fields)
        prompt = Template(settings.OUTPUT_FIELDS_SYSTEM_PROMPT).substitute(
            {
                "report": instance.text,
                "fields": generate_output_fields_prompt(output_fields),
            }
        )
        result = self.client.extract_data(prompt.strip(), Schema)
        instance.output = result.model_dump()
        instance.save()
