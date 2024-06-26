import logging
from typing import Iterator, override

from django.conf import settings
from openai import OpenAI

from radis.celery import app as celery_app
from radis.core.tasks import ProcessAnalysisJob, ProcessAnalysisTask
from radis.core.utils.chat_client import ChatClient
from radis.reports.models import Report
from radis.search.site import Search, SearchFilters
from radis.search.utils.query_parser import QueryParser

from .models import Answer, QuestionResult, RagJob, RagTask
from .site import retrieval_providers

logger = logging.getLogger(__name__)


class ProcessRagTask(ProcessAnalysisTask):
    analysis_task_class = RagTask

    def __init__(self) -> None:
        super().__init__()

        self._client = OpenAI(base_url=f"{settings.LLAMACPP_URL}/v1", api_key="none")

    @override
    def process_task(self, task: RagTask) -> None:
        report_body = task.report.body
        language = task.report.language.code

        if language not in settings.SUPPORTED_LANGUAGES:
            raise ValueError(f"Language '{language}' is not supported.")

        all_results: list[RagTask.Result] = []

        chat_client = ChatClient()

        for question in task.job.questions.all():
            llm_answer = chat_client.ask_yes_no_question(report_body, language, question.question)

            if llm_answer == "yes":
                answer = Answer.YES
            elif llm_answer == "no":
                answer = Answer.NO
            else:
                raise ValueError(f"Unexpected answer: {llm_answer}")

            result = (
                RagTask.Result.ACCEPTED
                if question.accepted_answer == answer
                else RagTask.Result.REJECTED
            )

            QuestionResult.objects.update_or_create(
                task=task,
                question=question,
                defaults={
                    "original_answer": answer,
                    "current_answer": answer,
                    "result": result,
                },
            )

            all_results.append(result)

            if all([result == RagTask.Result.ACCEPTED for result in all_results]):
                task.overall_result = RagTask.Result.ACCEPTED
            else:
                task.overall_result = RagTask.Result.REJECTED

            logger.info(
                "RAG task %s finished with overall result: %s",
                task,
                task.get_overall_result_display(),
            )

            task.save()


process_rag_task = ProcessRagTask()


celery_app.register_task(process_rag_task)


class ProcessRagJob(ProcessAnalysisJob):
    analysis_job_class = RagJob
    process_analysis_task = process_rag_task
    task_queue = "llm_queue"

    @override
    def collect_tasks(self, job: RagJob) -> Iterator[RagTask]:
        patient_sex = None
        if job.patient_sex == "M":
            patient_sex = "M"
        elif job.patient_sex == "F":
            patient_sex = "F"

        provider = job.provider
        retrieval_provider = retrieval_providers[provider]

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
                patient_sex=patient_sex,
                patient_age_from=job.age_from,
                patient_age_till=job.age_till,
            ),
        )

        logger.debug("Searching reports for task with search: %s", search)

        for document_id in retrieval_provider.retrieve(search):
            task = RagTask.objects.create(
                job=job, report=Report.objects.get(document_id=document_id)
            )
            yield task


process_rag_job = ProcessRagJob()

celery_app.register_task(process_rag_job)
