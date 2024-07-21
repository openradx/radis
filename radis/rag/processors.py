import asyncio
import logging
from asyncio import Semaphore

from channels.db import database_sync_to_async
from django import db
from django.conf import settings
from django.db.models.query import QuerySet
from pebble import concurrent

from radis.core.processors import AnalysisTaskProcessor
from radis.core.utils.chat_client import AsyncChatClient
from radis.reports.models import Report

from .models import Answer, Question, QuestionResult, RagInstance, RagTask

logger = logging.getLogger(__name__)


class RagTaskProcessor(AnalysisTaskProcessor):
    def process_task(self, task: RagTask) -> None:
        future = self.process_task_in_thread(task)
        future.result()

    @concurrent.thread
    def process_task_in_thread(self, task: RagTask) -> None:
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

    async def combine_reports(self, reports: QuerySet[Report]) -> Report:
        count = await reports.acount()
        if count > 1:
            raise ValueError("Multiple reports is not yet supported")

        report = await reports.afirst()
        if report is None:
            raise ValueError("No reports to combine")

        return report

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
