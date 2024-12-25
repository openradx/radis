from unittest.mock import patch

import pytest

from radis.chats.utils.chat_client import AsyncChatClient
from radis.chats.utils.testing_helpers import (
    create_async_openai_client_mock,
    create_question_body,
    create_report_body,
)


@pytest.mark.asyncio
async def test_ask_question():
    openai_mock = create_async_openai_client_mock("Fake Answer")

    with patch("openai.AsyncOpenAI", return_value=openai_mock):
        answer = await AsyncChatClient().ask_report_question(
            create_report_body(), create_question_body()
        )

        assert answer == "Fake Answer"
        assert openai_mock.chat.completions.create.call_count == 1


@pytest.mark.asyncio
async def test_ask_yes_no_question():
    openai_yes_mock = create_async_openai_client_mock("Yes")
    openai_no_mock = create_async_openai_client_mock("No")

    with patch("openai.AsyncOpenAI", return_value=openai_yes_mock):
        answer = await AsyncChatClient().ask_report_yes_no_question(
            create_report_body(), create_question_body()
        )

        assert answer == "yes"
        assert openai_yes_mock.chat.completions.create.call_count == 1

    with patch("openai.AsyncOpenAI", return_value=openai_no_mock):
        answer = await AsyncChatClient().ask_report_yes_no_question(
            create_report_body(), create_question_body()
        )

        assert answer == "no"
        assert openai_no_mock.chat.completions.create.call_count == 1
