import asyncio
from typing import Callable, ContextManager
from unittest.mock import MagicMock

import pytest
from faker import Faker


@pytest.fixture
def report_body() -> str:
    report_body = Faker().sentences(nb=40)
    return " ".join(report_body)


@pytest.fixture
def question_body() -> str:
    question_body = Faker().sentences(nb=1)
    return " ".join(question_body)


@pytest.fixture
def openai_chat_completions_mock() -> Callable[[str], ContextManager]:
    def _openai_chat_completions_mock(content: str) -> ContextManager:
        mock_openai = MagicMock()
        mock_response = MagicMock(choices=[MagicMock(message=MagicMock(content=content))])
        future = asyncio.Future()
        future.set_result(mock_response)
        mock_openai.chat.completions.create.return_value = future

        return mock_openai

    return _openai_chat_completions_mock
