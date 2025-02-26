import logging
from concurrent.futures import Future, ThreadPoolExecutor
from string import Template

from django import db
from django.conf import settings

from radis.chats.utils.chat_client import ChatClient
from radis.core.processors import AnalysisTaskProcessor
from radis.extractions.utils.processor_utils import (
    generate_output_fields_prompt,
    generate_output_fields_schema,
)

from .models import ExtractionInstance, ExtractionTask

logger = logging.getLogger(__name__)


class ExtractionTaskProcessor(AnalysisTaskProcessor):
    def __init__(self, task: ExtractionTask) -> None:
        super().__init__(task)
        self.client = ChatClient()

    def process_task(self, task: ExtractionTask) -> None:
        with ThreadPoolExecutor(max_workers=settings.EXTRACTION_LLM_CONCURRENCY_LIMIT) as executor:
            try:
                futures: list[Future] = []
                for instance in task.instances.all():
                    future = executor.submit(self.process_instance, instance)
                    futures.append(future)

                for future in futures:
                    future.result()

            finally:
                db.close_old_connections()

    def process_instance(self, instance: ExtractionInstance) -> None:
        assert not instance.is_processed
        instance.text = instance.report.body
        self.process_output_fields(instance)
        instance.is_processed = True
        instance.save()
        db.close_old_connections()

    def process_output_fields(self, instance: ExtractionInstance) -> None:
        job = instance.task.job
        Schema = generate_output_fields_schema(job.output_fields)
        prompt = Template(settings.OUTPUT_FIELDS_SYSTEM_PROMPT).substitute(
            {
                "report": instance.text,
                "fields": generate_output_fields_prompt(job.output_fields),
            }
        )
        result = self.client.extract_data(prompt.strip(), Schema)
        instance.output = result.model_dump()
        instance.save()
