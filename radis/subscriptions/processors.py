import logging
from concurrent.futures import Future, ThreadPoolExecutor
from string import Template

from adit_radis_shared.common.types import User
from django import db
from django.conf import settings

from radis.chats.utils.chat_client import ChatClient
from radis.core.processors import AnalysisTaskProcessor
from radis.reports.models import Report

from .models import (
    SubscribedItem,
    Subscription,
    SubscriptionTask,
)
from .utils.processor_utils import (
    generate_questions_for_prompt,
    generate_questions_schema,
)

logger = logging.getLogger(__name__)


class SubscriptionTaskProcessor(AnalysisTaskProcessor):
    def __init__(self, task: SubscriptionTask) -> None:
        super().__init__(task)
        self.client = ChatClient()

    def process_task(self, task: SubscriptionTask) -> None:
        user: User = task.job.owner
        active_group = user.active_group

        futures: list[Future] = []
        with ThreadPoolExecutor(max_workers=settings.EXTRACTION_LLM_CONCURRENCY_LIMIT) as executor:
            try:
                for report in task.reports.filter(groups=active_group):
                    future = executor.submit(self.process_report, report, task)
                    futures.append(future)

                for future in futures:
                    future.result()

            finally:
                db.close_old_connections()

    def process_report(self, report: Report, task: SubscriptionTask) -> None:
        subscription: Subscription = task.job.subscription
        Schema = generate_questions_schema(subscription.questions)
        prompt = Template(settings.QUESTION_SYSTEM_PROMPT).substitute(
            {
                "report": report.body,
                "questions": generate_questions_for_prompt(subscription.questions),
            }
        )
        result = self.client.extract_data(prompt, Schema)

        is_accepted = all(
            [getattr(result, field_name) for field_name in result.__pydantic_fields__]
        )
        if is_accepted:
            SubscribedItem.objects.create(
                subscription=task.job.subscription,
                job=task.job,
                report=report,
                filter_fields_results=result.model_dump(),
            )
            logger.debug(f"Report {report.pk} was accepted by subscription {subscription.pk}")
        else:
            logger.debug(f"Report {report.pk} was rejected by subscription {subscription.pk}")
