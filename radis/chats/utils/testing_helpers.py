import asyncio
from unittest.mock import MagicMock

import httpx
import openai
from faker import Faker
from pydantic import BaseModel


def create_report_body() -> str:
    report_body = Faker().sentences(nb=40)
    return " ".join(report_body)


def create_question_body() -> str:
    question_body = Faker().sentences(nb=1)
    return " ".join(question_body)


def create_async_openai_client_mock(content: str | None) -> openai.AsyncOpenAI:
    openai_mock = MagicMock()
    mock_response = MagicMock(choices=[MagicMock(message=MagicMock(content=content))])
    future = asyncio.Future()
    future.set_result(mock_response)
    openai_mock.chat.completions.create.return_value = future
    return openai_mock


def create_openai_client_mock(content: BaseModel | None) -> openai.OpenAI:
    openai_mock = MagicMock()
    mock_response = MagicMock(choices=[MagicMock(message=MagicMock(parsed=content))])
    openai_mock.beta.chat.completions.parse.return_value = mock_response
    return openai_mock


def make_rate_limit_error(headers: dict[str, str] | None = None) -> openai.RateLimitError:
    """Build a real openai.RateLimitError carrying chosen response headers (e.g. retry-after)."""
    request = httpx.Request("POST", "http://testserver/v1/chat/completions")
    response = httpx.Response(429, headers=headers or {}, request=request)
    return openai.RateLimitError("rate limited", response=response, body=None)


def make_connection_error() -> openai.APIConnectionError:
    """Build a real openai.APIConnectionError (a transient, non-429 error)."""
    request = httpx.Request("POST", "http://testserver/v1/chat/completions")
    return openai.APIConnectionError(message="connection failed", request=request)
