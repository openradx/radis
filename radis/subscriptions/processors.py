import logging
from concurrent.futures import Future, ThreadPoolExecutor
from string import Template
from typing import Any

from adit_radis_shared.common.types import User
from django import db
from django.conf import settings
from pydantic import ValidationError

from radis.chats.utils.chat_client import ChatClient
from radis.core.processors import AnalysisTaskProcessor
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
        try:
            subscription: Subscription = task.job.subscription

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

                try:
                    filter_response = self.client.extract_data(filter_prompt, filter_schema)

                    for question in filter_questions:
                        field_name = get_filter_question_field_name(question)
                        answer = getattr(filter_response, field_name, None)
                        if answer is None:
                            logger.debug(
                                f"LLM returned None for question {question.pk} ",
                                f"on report {report.pk}",
                            )
                            is_accepted = False
                            break
                        else:
                            answer_bool = bool(answer)
                            filter_results[str(question.pk)] = answer_bool
                            if answer_bool != question.expected_answer_bool:
                                is_accepted = False
                                break
                except RuntimeError as e:
                    logger.error(f"LLM API error filtering report {report.pk}: {e}")
                    return
                except ValidationError as e:
                    logger.error(f"Response validation failed filtering report {report.pk}: {e}")
                    return
                except ValueError:
                    logger.error(f"No parsed response received filtering report {report.pk}")
                    return
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

                try:
                    extraction_response = self.client.extract_data(
                        extraction_prompt, extraction_schema
                    )

                    for field in output_fields:
                        extraction_results[str(field.pk)] = getattr(
                            extraction_response, get_output_field_name(field), None
                        )
                except RuntimeError as e:
                    logger.error(f"LLM API error extracting from report {report.pk}: {e}")
                    return
                except ValidationError as e:
                    logger.error(
                        f"Response validation failed extracting from report {report.pk}: {e}"
                    )
                    return
                except ValueError:
                    logger.error(f"No parsed response received extracting from report {report.pk}")
                    return

            SubscribedItem.objects.create(
                subscription=task.job.subscription,
                job=task.job,
                report=report,
                filter_results=filter_results or None,
                extraction_results=extraction_results or None,
            )
            logger.debug(f"Report {report.pk} was accepted by subscription {subscription.pk}")
        finally:
            db.close_old_connections()
