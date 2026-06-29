from unittest.mock import MagicMock, patch

import pytest
from pydantic import create_model

from radis.chats.utils.testing_helpers import create_openai_client_mock
from radis.subscriptions.models import SubscribedItem
from radis.subscriptions.processors import SubscriptionTaskProcessor
from radis.subscriptions.utils.processor_utils import (
    get_filter_question_field_name,
    get_output_field_name,
)
from radis.subscriptions.utils.testing_helpers import create_subscription_task


@pytest.mark.django_db(transaction=True)
def test_subscription_task_processor_filters_and_extracts():
    task, filter_question, output_field, report = create_subscription_task()

    filter_field_name = get_filter_question_field_name(filter_question)
    extraction_field_name = get_output_field_name(output_field)
    filter_field_definitions = {}
    filter_field_definitions[filter_field_name] = (bool, ...)

    extraction_field_definitions = {}
    extraction_field_definitions[extraction_field_name] = (str, ...)

    FilterOutput = create_model("FilterOutput", **filter_field_definitions)
    ExtractionOutput = create_model("ExtractionOutput", **extraction_field_definitions)

    filter_output = FilterOutput(**{filter_field_name: True})
    extraction_output = ExtractionOutput(**{extraction_field_name: "Pneumothorax status confirmed"})

    filter_response = MagicMock(choices=[MagicMock(message=MagicMock(parsed=filter_output))])
    extraction_response = MagicMock(
        choices=[MagicMock(message=MagicMock(parsed=extraction_output))]
    )

    openai_mock = create_openai_client_mock(extraction_output)
    openai_mock.beta.chat.completions.parse = MagicMock(
        side_effect=[filter_response, extraction_response]
    )
    with patch("openai.OpenAI", return_value=openai_mock):
        SubscriptionTaskProcessor(task).start()

    subscribed_item = SubscribedItem.objects.get(subscription=task.job.subscription, report=report)
    assert subscribed_item.filter_results == {str(filter_question.pk): True}
    assert subscribed_item.extraction_results == {
        str(output_field.pk): "Pneumothorax status confirmed"
    }


@pytest.mark.django_db(transaction=True)
def test_subscription_task_processor_handles_llm_null_response():
    task, _, _, report = create_subscription_task()

    processor = SubscriptionTaskProcessor(task)
    processor.client.extract_data = MagicMock(return_value=None)

    processor.start()

    assert not SubscribedItem.objects.filter(
        subscription=task.job.subscription, report=report
    ).exists()
    processor.client.extract_data.assert_called_once()


@pytest.mark.django_db(transaction=True)
def test_subscription_task_processor_with_no_expected_answer():
    task, filter_question, _, report = create_subscription_task()

    filter_field_name = get_filter_question_field_name(filter_question)
    filter_response = MagicMock()
    setattr(filter_response, filter_field_name, None)

    processor = SubscriptionTaskProcessor(task)
    processor.client.extract_data = MagicMock(return_value=filter_response)

    processor.start()

    assert not SubscribedItem.objects.filter(
        subscription=task.job.subscription, report=report
    ).exists()
    processor.client.extract_data.assert_called_once()


@pytest.mark.django_db(transaction=True)
def test_subscription_task_processor_extraction_only():
    task, _, output_field, report = create_subscription_task()
    task.job.subscription.filter_questions.all().delete()

    extraction_field_name = get_output_field_name(output_field)
    extraction_field_definitions = {}
    extraction_field_definitions[extraction_field_name] = (str, ...)

    ExtractionOutput = create_model("ExtractionOnlyOutput", **extraction_field_definitions)
    extraction_output = ExtractionOutput(**{extraction_field_name: "Only extraction response"})

    openai_mock = create_openai_client_mock(extraction_output)
    with patch("openai.OpenAI", return_value=openai_mock):
        SubscriptionTaskProcessor(task).start()

    subscribed_item = SubscribedItem.objects.get(subscription=task.job.subscription, report=report)
    assert subscribed_item.filter_results is None
    assert subscribed_item.extraction_results == {str(output_field.pk): "Only extraction response"}


@pytest.mark.django_db(transaction=True)
def test_subscription_task_processor_filter_only():
    task, filter_question, output_field, report = create_subscription_task()
    output_field.delete()

    filter_field_name = get_filter_question_field_name(filter_question)
    filter_field_definitions = {}
    filter_field_definitions[filter_field_name] = (bool, ...)
    FilterOutput = create_model("FilterOnlyOutput", **filter_field_definitions)
    filter_output = FilterOutput(**{filter_field_name: True})

    openai_mock = create_openai_client_mock(filter_output)
    with patch("openai.OpenAI", return_value=openai_mock):
        SubscriptionTaskProcessor(task).start()

    subscribed_item = SubscribedItem.objects.get(subscription=task.job.subscription, report=report)
    assert subscribed_item.filter_results == {str(filter_question.pk): True}
    assert subscribed_item.extraction_results is None
