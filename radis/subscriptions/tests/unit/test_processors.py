from unittest.mock import MagicMock, patch

import pytest
from pydantic import BaseModel

from radis.chats.utils.testing_helpers import create_openai_client_mock
from radis.subscriptions.models import SubscribedItem
from radis.subscriptions.processors import SubscriptionTaskProcessor
from radis.subscriptions.utils.testing_helpers import create_subscription_task


class FilterOutput(BaseModel):
    filter_0: bool


class ExtractionOutput(BaseModel):
    extraction_0: str


@pytest.mark.django_db(transaction=True)
def test_subscription_task_processor_filters_and_extracts():
    task, filter_question, extraction_field, report = create_subscription_task()

    filter_output = FilterOutput(filter_0=True)
    extraction_output = ExtractionOutput(extraction_0="Pneumothorax status confirmed")

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
        str(extraction_field.pk): "Pneumothorax status confirmed"
    }
