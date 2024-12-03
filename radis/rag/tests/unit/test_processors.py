from unittest.mock import patch

import pytest
from django.db import close_old_connections

from radis.rag.models import Answer, RagInstance
from radis.rag.processors import RagTaskProcessor


@pytest.mark.django_db(transaction=True)
def test_rag_task_processor(
    create_rag_task, openai_chat_completions_mock, mocker, default_grammars
):
    num_rag_instances = 5
    num_questions = 5
    rag_task = create_rag_task(
        language_code="en",
        num_questions=num_questions,
        accepted_answer=Answer.YES,
        num_rag_instances=num_rag_instances,
    )

    openai_mock = openai_chat_completions_mock("Yes")
    process_rag_task_spy = mocker.spy(RagTaskProcessor, "process_task")
    process_rag_instance_spy = mocker.spy(RagTaskProcessor, "process_rag_instance")
    process_filter_question_spy = mocker.spy(RagTaskProcessor, "process_filter_question")

    with patch("openai.AsyncOpenAI", return_value=openai_mock):
        RagTaskProcessor(rag_task).start()

        for instance in rag_task.rag_instances.all():
            assert instance.overall_result == RagInstance.Result.ACCEPTED
            question_results = instance.filter_results.all()
            assert all(
                [result.result == RagInstance.Result.ACCEPTED for result in question_results]
            )
            assert all([result.original_answer == Answer.YES for result in question_results])

        assert process_rag_task_spy.call_count == 1
        assert process_rag_instance_spy.call_count == num_rag_instances
        assert process_filter_question_spy.call_count == num_rag_instances * num_questions
        assert openai_mock.chat.completions.create.call_count == num_rag_instances * num_questions

    close_old_connections()
