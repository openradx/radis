import logging
from typing import Any
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
    build_extraction_schema,
    build_filter_schema,
    generate_extraction_fields_prompt,
    generate_filter_questions_prompt,
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
        filter_bundle = build_filter_schema(subscription.filter_questions)

        filter_results: dict[str, bool] = {}
        is_accepted = True

        if filter_bundle.mapping:
            filter_prompt = Template(settings.SUBSCRIPTION_FILTER_PROMPT).substitute(
                {
                    "report": report.body,
                    "questions": generate_filter_questions_prompt(filter_bundle.mapping),
                }
            )
            filter_response = self.client.extract_data(filter_prompt, filter_bundle.schema)

            for field_name, question in filter_bundle.mapping:
                answer = bool(getattr(filter_response, field_name))
                filter_results[str(question.pk)] = answer
                if answer != question.expected_answer_bool:
                    is_accepted = False
        else:
            logger.debug(
                "Subscription %s has no filter questions; accepting report %s by default",
                subscription.pk,
                report.pk,
            )

        if not is_accepted:
            logger.debug(f"Report {report.pk} was rejected by subscription {subscription.pk}")
            return

        extraction_bundle = build_extraction_schema(subscription.extraction_fields)
        extraction_results: dict[str, Any] = {}

        if extraction_bundle.mapping:
            extraction_prompt = Template(settings.SUBSCRIPTION_EXTRACTION_PROMPT).substitute(
                {
                    "report": report.body,
                    "fields": generate_extraction_fields_prompt(extraction_bundle.mapping),
                }
            )
            extraction_response = self.client.extract_data(
                extraction_prompt, extraction_bundle.schema
            )

            for field_name, field in extraction_bundle.mapping:
                extraction_results[str(field.pk)] = getattr(extraction_response, field_name)

        SubscribedItem.objects.create(
            subscription=task.job.subscription,
            job=task.job,
            report=report,
            filter_results=filter_results or None,
            extraction_results=extraction_results or None,
        )
        logger.debug(f"Report {report.pk} was accepted by subscription {subscription.pk}")
