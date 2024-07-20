import asyncio
import logging
from asyncio import Semaphore
from itertools import batched
from typing import Iterator, override

from channels.db import database_sync_to_async
from django import db
from django.conf import settings
from django.db.models.query import QuerySet

from radis.celery import app as celery_app
from radis.core.tasks import ProcessAnalysisJob, ProcessAnalysisTask
from radis.core.utils.chat_client import AsyncChatClient
from radis.reports.models import Report
from radis.search.site import Search, SearchFilters
from radis.search.utils.query_parser import QueryParser

from .models import Answer, Question, QuestionResult, RagInstance, RagJob, RagTask
from .site import retrieval_providers

logger = logging.getLogger(__name__)


class ProcessRagTask(ProcessAnalysisTask):
    analysis_task_class = RagTask

    def __init__(self) -> None:
        super().__init__()

    @override
    def process_task(self, task: RagTask) -> None:
        asyncio.run(self.process_rag_task(task))

    async def process_rag_task(self, task: RagTask) -> None:
        client = AsyncChatClient()
        sem = Semaphore(settings.RAG_LLM_CONCURRENCY_LIMIT)

        await asyncio.gather(
            *[
                self.process_rag_instance(rag_instance, client, sem)
                async for rag_instance in task.rag_instances.prefetch_related("reports")
            ]
        )
        await database_sync_to_async(db.close_old_connections)()

    async def process_rag_instance(
        self, rag_instance: RagInstance, client: AsyncChatClient, sem: Semaphore
    ) -> None:
        report = await self.combine_reports(rag_instance.reports.prefetch_related("language"))
        language = report.language

        if language.code not in settings.SUPPORTED_LANGUAGES:
            raise ValueError(f"Language '{language}' is not supported.")

        async with sem:
            results = await asyncio.gather(
                *[
                    self.process_yes_or_no_question(
                        rag_instance, report.body, language.code, question, client
                    )
                    async for question in rag_instance.task.job.questions.all()
                ]
            )

        if all([result == RagInstance.Result.ACCEPTED for result in results]):
            overall_result = RagInstance.Result.ACCEPTED
        else:
            overall_result = RagInstance.Result.REJECTED

        rag_instance.overall_result = overall_result
        await rag_instance.asave()

        logger.info(
            "Overall RAG result for for report %s: %s",
            rag_instance,
            rag_instance.get_overall_result_display(),
        )

    async def process_yes_or_no_question(
        self,
        rag_instance: RagInstance,
        body: str,
        language: str,
        question: Question,
        client: AsyncChatClient,
    ) -> RagInstance.Result:
        llm_answer = await client.ask_yes_no_question(body, language, question.question)

        if llm_answer == "yes":
            answer = Answer.YES
        elif llm_answer == "no":
            answer = Answer.NO
        else:
            raise ValueError(f"Unexpected answer: {llm_answer}")

        result = (
            RagInstance.Result.ACCEPTED
            if question.accepted_answer == answer
            else RagInstance.Result.REJECTED
        )

        await QuestionResult.objects.aupdate_or_create(
            rag_instance=rag_instance,
            question=question,
            defaults={
                "original_answer": answer,
                "current_answer": answer,
                "result": result,
            },
        )

        logger.debug("RAG result for question %s: %s", question, answer)

        return result

    async def combine_reports(self, reports: QuerySet[Report]) -> Report:
        count = await reports.acount()
        if count > 1:
            raise ValueError("Multiple reports is not yet supported")

        report = await reports.afirst()
        if report is None:
            raise ValueError("No reports to combine")

        return report


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

        for document_ids in batched(
            retrieval_provider.retrieve(search), settings.RAG_TASK_BATCH_SIZE
        ):
            logger.debug("Creating RAG task for document IDs: %s", document_ids)
            task = RagTask.objects.create(job=job)
            for document_id in document_ids:
                rag_instance = RagInstance.objects.create(task=task)
                rag_instance.reports.add(Report.objects.get(document_id=document_id))

            yield task


process_rag_job = ProcessRagJob()

celery_app.register_task(process_rag_job)
