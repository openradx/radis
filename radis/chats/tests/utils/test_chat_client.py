from unittest.mock import patch

import pytest

from radis.chats.grammars import FreeTextGrammar, YesNoGrammar
from radis.chats.utils.chat_client import AsyncChatClient


@pytest.mark.asyncio
async def test_ask_question(report_body, question_body, openai_chat_completions_mock):
    openai_mock = openai_chat_completions_mock("Fake Answer")

    with patch("openai.AsyncOpenAI", return_value=openai_mock):
        answer = await AsyncChatClient().ask_report_question(
            context=report_body,
            question=question_body,
            grammar=FreeTextGrammar,
        )

        assert answer == "Fake Answer"
        assert openai_mock.chat.completions.create.call_count == 1


@pytest.mark.asyncio
async def test_ask_yes_no_question(report_body, question_body, openai_chat_completions_mock):
    openai_yes_mock = openai_chat_completions_mock("Yes")
    openai_no_mock = openai_chat_completions_mock("No")

    with patch("openai.AsyncOpenAI", return_value=openai_yes_mock):
        answer = await AsyncChatClient().ask_report_question(
            context=report_body, question=question_body, grammar=YesNoGrammar
        )

        assert answer == "yes"
        assert openai_yes_mock.chat.completions.create.call_count == 1

    with patch("openai.AsyncOpenAI", return_value=openai_no_mock):
        answer = await AsyncChatClient().ask_report_question(
            context=report_body, question=question_body, grammar=YesNoGrammar
        )

        assert answer == "no"
        assert openai_no_mock.chat.completions.create.call_count == 1
