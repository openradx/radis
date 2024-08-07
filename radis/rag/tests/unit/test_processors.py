from unittest.mock import patch

import pytest
from channels.db import database_sync_to_async
from django.db import close_old_connections

from radis.rag.models import Answer, RagInstance
from radis.rag.processors import RagTaskProcessor


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_rag_task_processor(create_rag_task, openai_chat_completions_mock, mocker):
    num_rag_instances = 5
    num_questions = 5
    rag_task = await database_sync_to_async(create_rag_task)(
        language_code="en",
        num_questions=num_questions,
        accepted_answer="Y",
        num_rag_instances=num_rag_instances,
    )

    openai_mock = openai_chat_completions_mock("Yes")
    process_rag_task_spy = mocker.spy(RagTaskProcessor, "process_task")
    process_rag_instance_spy = mocker.spy(RagTaskProcessor, "process_rag_instance")
    process_yes_or_no_question_spy = mocker.spy(RagTaskProcessor, "process_yes_or_no_question")

    with patch("openai.AsyncOpenAI", return_value=openai_mock):
        await RagTaskProcessor(rag_task).start()
        rag_instances = rag_task.rag_instances.all()

        for instance in rag_instances:
            assert instance.overall_result == RagInstance.Result.ACCEPTED
            question_results = instance.results.all()
            assert all(
                [result.result == RagInstance.Result.ACCEPTED for result in question_results]
            )
            assert all([result.original_answer == Answer.YES for result in question_results])

            assert process_rag_task_spy.call_count == 1
            assert process_rag_instance_spy.call_count == num_rag_instances
            assert process_yes_or_no_question_spy.call_count == num_rag_instances * num_questions
            assert (
                openai_mock.chat.completions.create.call_count == num_rag_instances * num_questions
            )

    close_old_connections()
