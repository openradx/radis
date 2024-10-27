import asyncio
import logging
from asyncio import Semaphore

from channels.db import database_sync_to_async
from django import db
from django.conf import settings
from django.db.models import Prefetch

from radis.chats.grammars import YesNoGrammar, predefined_grammars
from radis.chats.utils.chat_client import AsyncChatClient
from radis.core.processors import AnalysisTaskProcessor

from .models import (
    AnalysisQuestion,
    AnalysisQuestionResult,
    Answer,
    FilterQuestion,
    FilterQuestionResult,
    RagInstance,
    RagTask,
)

logger = logging.getLogger(__name__)


class RagTaskProcessor(AnalysisTaskProcessor):
    def process_task(self, task: RagTask) -> None:
        asyncio.run(self.process_task_async(task))

    async def process_task_async(self, task: RagTask) -> None:
        client = AsyncChatClient()
        sem = Semaphore(settings.RAG_LLM_CONCURRENCY_LIMIT)

        await asyncio.gather(
            *[
                self.process_rag_instance(rag_instance, client, sem)
                async for rag_instance in task.rag_instances.prefetch_related(
                    Prefetch("report"), Prefetch("other_reports")
                )
            ]
        )
        await database_sync_to_async(db.close_old_connections)()

    async def process_rag_instance(
        self, rag_instance: RagInstance, client: AsyncChatClient, sem: Semaphore
    ) -> None:
        rag_instance.text = await self.get_text_to_analyze(rag_instance)
        await rag_instance.asave()

        async with sem:
            # Process filter questions
            results = await asyncio.gather(
                *[
                    self.process_filter_question(rag_instance, question, client)
                    async for question in rag_instance.task.job.filter_questions.all()
                ]
            )

            # Process analysis questions
            await asyncio.gather(
                *[
                    self.process_analysis_question(rag_instance, question, client)
                    async for question in rag_instance.task.job.analysis_questions.all()
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

    async def process_filter_question(
        self,
        rag_instance: RagInstance,
        question: FilterQuestion,
        client: AsyncChatClient,
    ) -> RagInstance.Result:
        llm_answer = await client.ask_report_question(
            context=rag_instance.text, question=question.question, grammar=YesNoGrammar
        )

        answer = Answer.YES if llm_answer == "yes" else Answer.NO

        result = (
            RagInstance.Result.ACCEPTED
            if question.accepted_answer == answer
            else RagInstance.Result.REJECTED
        )

        await FilterQuestionResult.objects.aupdate_or_create(
            rag_instance=rag_instance,
            question=question,
            defaults={
                "original_answer": answer,
                "current_answer": answer,
                "result": result,
            },
        )

        logger.debug("RAG result for filter question %s: %s", question, answer)

        return result

    async def process_analysis_question(
        self,
        rag_instance: RagInstance,
        question: AnalysisQuestion,
        client: AsyncChatClient,
    ) -> None:
        try:
            grammar = predefined_grammars[question.grammar]
        except KeyError:
            raise ValueError(f"Unknown grammar: {question.grammar}")

        llm_answer = await client.ask_report_question(
            context=rag_instance.text,
            question=question.question,
            grammar=grammar,
        )

        await AnalysisQuestionResult.objects.aupdate_or_create(
            rag_instance=rag_instance,
            question=question,
            defaults={
                "original_answer": llm_answer,
                "current_answer": llm_answer,
            },
        )

        logger.debug("RAG result for analysis question %s: %s", question, llm_answer)
