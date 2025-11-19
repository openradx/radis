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
