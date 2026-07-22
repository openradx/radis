import logging
from concurrent.futures import Future, ThreadPoolExecutor
from string import Template
from typing import Any

from adit_radis_shared.common.types import User
from django import db
from django.conf import settings

from radis.core.processors import AnalysisTaskProcessor
from radis.core.utils.llm_client import LLMClient
from radis.extractions.utils.processor_utils import (
    generate_output_fields_prompt,
    generate_output_fields_schema,
)
from radis.reports.models import Report

from .models import (
    SubscribedItem,
    Subscription,
    SubscriptionTask,
)
from .utils.processor_utils import (
    generate_filter_questions_prompt,
    generate_filter_questions_schema,
    get_filter_question_field_name,
    get_output_field_name,
)

logger = logging.getLogger(__name__)


class SubscriptionTaskProcessor(AnalysisTaskProcessor):
    def __init__(self, task: SubscriptionTask) -> None:
        super().__init__(task)
        self.client = LLMClient()

    def process_task(self, task: SubscriptionTask) -> None:
        user: User = task.job.owner
        active_group = user.active_group

        futures: list[Future] = []
        with ThreadPoolExecutor(max_workers=settings.EXTRACTION_LLM_CONCURRENCY_LIMIT) as executor:
            try:
                for report in task.reports.filter(groups=active_group):
                    future = executor.submit(self.process_report, report, task)
                    futures.append(future)

                exceptions: list[Exception] = []
                for future in futures:
                    try:
                        future.result()
                    except Exception as e:
                        logger.error("Error processing report in subscription task: %s", e)
                        exceptions.append(e)

                if exceptions:
                    raise exceptions[0]

            finally:
                db.close_old_connections()

    def process_report(self, report: Report, task: SubscriptionTask) -> None:
        # Runs in a worker thread; its connection must be closed in that same
        # thread (the close in process_task only reaches the main thread).
        try:
            subscription: Subscription = task.job.subscription

            # A report can be re-selected when its updated_at is bumped again or
            # when a partially failed task is retried; skip it (and the LLM
            # cost) if it is already in the inbox.
            if SubscribedItem.objects.filter(subscription=subscription, report=report).exists():
                logger.debug(
                    "Report %s already subscribed for subscription %s - skipping",
                    report.pk,
                    subscription.pk,
                )
                return

            filter_results: dict[str, bool] = {}
            is_accepted = True

            filter_questions = list(subscription.filter_questions.order_by("pk"))

            if filter_questions:
                filter_prompt = Template(settings.SUBSCRIPTION_FILTER_PROMPT).substitute(
                    {
                        "report": report.body,
                        "questions": generate_filter_questions_prompt(filter_questions),
                    }
                )
                filter_schema = generate_filter_questions_schema(filter_questions)

                # LLM/validation errors deliberately propagate: they fail the
                # task (visible, retryable), and the retry skips reports that
                # already produced a SubscribedItem. Swallowing them here would
                # silently drop the report forever, because last_refreshed has
                # already advanced past it.
                filter_response = self.client.extract_data(filter_prompt, filter_schema)

                for question in filter_questions:
                    field_name = get_filter_question_field_name(question)
                    answer = getattr(filter_response, field_name, None)
                    if answer is None:
                        logger.warning(
                            "LLM returned None for question %s on report %s",
                            question.pk,
                            report.pk,
                        )
                        is_accepted = False
                        break
                    else:
                        answer_bool = bool(answer)
                        filter_results[str(question.pk)] = answer_bool
                        if answer_bool != question.expected_answer_bool:
                            is_accepted = False
                            break
            else:
                logger.debug(
                    "Subscription %s has no filter questions; accepting report %s by default",
                    subscription.pk,
                    report.pk,
                )

            if not is_accepted:
                logger.debug(f"Report {report.pk} was rejected by subscription {subscription.pk}")
                return

            extraction_results: dict[str, Any] = {}
            output_fields = list(subscription.output_fields.order_by("pk"))

            if output_fields:
                extraction_prompt = Template(settings.SUBSCRIPTION_EXTRACTION_PROMPT).substitute(
                    {
                        "report": report.body,
                        "fields": generate_output_fields_prompt(output_fields),
                    }
                )
                extraction_schema = generate_output_fields_schema(output_fields)

                extraction_response = self.client.extract_data(extraction_prompt, extraction_schema)

                for field in output_fields:
                    extraction_results[str(field.pk)] = getattr(
                        extraction_response, get_output_field_name(field), None
                    )

            SubscribedItem.objects.create(
                subscription=task.job.subscription,
                job=task.job,
                report=report,
                filter_results=filter_results or None,
                extraction_results=extraction_results or None,
            )
            logger.debug(f"Report {report.pk} was accepted by subscription {subscription.pk}")
        finally:
            db.connections.close_all()
