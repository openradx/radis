import asyncio
import logging
from asyncio import Semaphore

from channels.db import database_sync_to_async
from django import db
from django.conf import settings
from django.db.models import Prefetch

from radis.core.processors import AnalysisTaskProcessor
from radis.core.utils.chat_client import AsyncChatClient

from .models import Answer, Question, QuestionResult, RagInstance, RagTask

logger = logging.getLogger(__name__)


class RagTaskProcessor(AnalysisTaskProcessor):
    def process_task(self, task: RagTask) -> None:
        asyncio.run(self.process_task_async(task))

    async def process_task_async(self, task: RagTask) -> None:
        language_code = task.job.language.code
        client = AsyncChatClient()
        sem = Semaphore(settings.RAG_LLM_CONCURRENCY_LIMIT)

        await asyncio.gather(
            *[
                self.process_rag_instance(rag_instance, language_code, client, sem)
                async for rag_instance in task.rag_instances.prefetch_related(
                    Prefetch("report"), Prefetch("other_reports")
                )
            ]
        )
        await database_sync_to_async(db.close_old_connections)()

    async def process_rag_instance(
        self, rag_instance: RagInstance, language_code: str, client: AsyncChatClient, sem: Semaphore
    ) -> None:
        rag_instance.text = await self.get_text_to_analyze(rag_instance)
        await rag_instance.asave()

        if language_code not in settings.SUPPORTED_LANGUAGES:
            raise ValueError(f"Language '{language_code}' is not supported.")

        async with sem:
            results = await asyncio.gather(
                *[
                    self.process_yes_or_no_question(rag_instance, language_code, question, client)
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

    async def get_text_to_analyze(self, rag_instance: RagInstance) -> str:
        text_to_analyze = ""
        text_to_analyze += rag_instance.report.body

        async for report in rag_instance.other_reports.order_by("study_datetime").all():
            if text_to_analyze:
                text_to_analyze += "\n\n"
            text_to_analyze += report.body

        return text_to_analyze

    async def process_yes_or_no_question(
        self,
        rag_instance: RagInstance,
        language: str,
        question: Question,
        client: AsyncChatClient,
    ) -> RagInstance.Result:
        llm_answer = await client.ask_yes_no_question(
            rag_instance.text, language, question.question
        )

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
