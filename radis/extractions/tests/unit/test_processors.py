from unittest.mock import patch

import pytest
from django.db import close_old_connections
from pytest_mock import MockerFixture

from radis.chats.utils.testing_helpers import create_async_openai_client_mock
from radis.extractions.processors import ExtractionTaskProcessor
from radis.extractions.utils.testing_helpers import create_extraction_task


@pytest.mark.django_db(transaction=True)
def test_extraction_task_processor(mocker: MockerFixture):
    num_output_fields = 5
    num_extraction_instances = 5
    task = create_extraction_task(
        language_code="en",
        num_output_fields=num_output_fields,
        num_extraction_instances=num_extraction_instances,
    )

    openai_mock = create_async_openai_client_mock("Yes")
    process_extraction_task_spy = mocker.spy(ExtractionTaskProcessor, "process_task")
    process_extraction_instance_spy = mocker.spy(
        ExtractionTaskProcessor, "process_extraction_instance"
    )
    process_output_fields_spy = mocker.spy(ExtractionTaskProcessor, "process_output_fields")

    with patch("openai.AsyncOpenAI", return_value=openai_mock):
        ExtractionTaskProcessor(task).start()

        for instance in task.instances.all():
            assert instance.is_processed

        assert process_extraction_task_spy.call_count == 1
        assert process_extraction_instance_spy.call_count == num_extraction_instances
        assert process_output_fields_spy.call_count == num_extraction_instances * num_output_fields
        assert (
            openai_mock.chat.completions.create.call_count
            == num_extraction_instances * num_output_fields
        )

    close_old_connections()
