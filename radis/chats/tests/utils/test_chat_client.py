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
        answer = await AsyncChatClient().chat_about_report(
            create_report_body(), create_question_body()
        )

        assert answer == "Fake Answer"
        assert openai_mock.chat.completions.create.call_count == 1
