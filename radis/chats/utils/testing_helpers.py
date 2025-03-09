import asyncio
from unittest.mock import MagicMock

import openai
from faker import Faker
from pydantic import BaseModel


def create_report_body() -> str:
    report_body = Faker().sentences(nb=40)
    return " ".join(report_body)


def create_question_body() -> str:
    question_body = Faker().sentences(nb=1)
    return " ".join(question_body)


def create_async_openai_client_mock(content: str) -> openai.AsyncOpenAI:
    openai_mock = MagicMock()
    mock_response = MagicMock(choices=[MagicMock(message=MagicMock(content=content))])
    future = asyncio.Future()
    future.set_result(mock_response)
    openai_mock.chat.completions.create.return_value = future
    return openai_mock


def create_openai_client_mock(content: BaseModel) -> openai.OpenAI:
    openai_mock = MagicMock()
    mock_response = MagicMock(choices=[MagicMock(message=MagicMock(parsed=content))])
    openai_mock.beta.chat.completions.parse.return_value = mock_response
    return openai_mock
