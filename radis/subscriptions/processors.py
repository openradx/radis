import asyncio
import logging
from asyncio import Semaphore
from typing import List

from channels.db import database_sync_to_async
from django import db
from django.conf import settings
from django.db.models import Prefetch, QuerySet

from radis.core.processors import AnalysisTaskProcessor
from radis.core.utils.chat_client import AsyncChatClient
from radis.reports.models import Language, Report

from .models import Answer, RagResult, SubscribedItem, SubscriptionQuestion, SubscriptionTask

logger = logging.getLogger(__name__)


class SubscriptionTaskProcessor(AnalysisTaskProcessor):
    def process_task(self, task: SubscriptionTask) -> None:
        asyncio.run(self.process_task_async(task))

    async def process_task_async(self, task: SubscriptionTask) -> None:
        client = AsyncChatClient()
        sem = Semaphore(settings.RAG_LLM_CONCURRENCY_LIMIT)

        questions = task.job.subscription.questions.all()

        await asyncio.gather(
            *[
                self.process_report(task, report, questions, sem, client)
                async for report in task.reports.prefetch_related(Prefetch("language"))
            ]
        )
        await database_sync_to_async(db.close_old_connections)()

    async def process_report(
        self,
        task: SubscriptionTask,
        report: Report,
        questions: QuerySet[SubscriptionQuestion],
        sem: Semaphore,
        client: AsyncChatClient,
    ) -> None:
        if report.language.code not in settings.SUPPORTED_LANGUAGES:
            raise ValueError(f"Language '{report.language.code}' is not supported.")

        async with sem:
            results: List[RagResult] = await asyncio.gather(
                *[
                    self.process_yes_or_no_question(report.body, report.language, question, client)
                    async for question in questions
                ]
            )

        overall_result = (
            RagResult.ACCEPTED
            if all([result == RagResult.ACCEPTED for result in results])
            else RagResult.REJECTED
        )

        if overall_result == RagResult.ACCEPTED:
            await SubscribedItem.objects.acreate(
                subscription=task.job.subscription,
                report=report,
            )

        logger.debug(f"Report {report.id} processed with result {overall_result}")

    async def process_yes_or_no_question(
        self,
        report_body: str,
        language: Language,
        question: SubscriptionQuestion,
        client: AsyncChatClient,
    ) -> RagResult:
        llm_answer = await client.ask_yes_no_question(report_body, language.code, question.question)

        if llm_answer == "yes":
            answer = Answer.YES
        elif llm_answer == "no":
            answer = Answer.NO
        else:
            raise ValueError(f"Invalid answer from LLM: {llm_answer}")

        rag_result = (
            RagResult.ACCEPTED if answer == question.accepted_answer else RagResult.REJECTED
        )

        return rag_result
