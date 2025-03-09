from unittest.mock import patch

import pytest
from pydantic import BaseModel
from pytest_mock import MockerFixture

from radis.chats.utils.testing_helpers import (
    create_openai_client_mock,
)
from radis.extractions.processors import ExtractionTaskProcessor
from radis.extractions.utils.testing_helpers import create_extraction_task


class Output(BaseModel):
    foo: str


@pytest.mark.django_db(transaction=True)
def test_extraction_task_processor(mocker: MockerFixture):
    num_output_fields = 5
    num_extraction_instances = 5
    task = create_extraction_task(
        language_code="en",
        num_output_fields=num_output_fields,
        num_extraction_instances=num_extraction_instances,
    )

    output = Output(foo="bar")
    openai_mock = create_openai_client_mock(output)
    with patch("openai.OpenAI", return_value=openai_mock):
        ExtractionTaskProcessor(task).start()

        for instance in task.instances.all():
            assert instance.is_processed
            assert instance.output == output.model_dump()
