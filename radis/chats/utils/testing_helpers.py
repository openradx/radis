import asyncio
from unittest.mock import MagicMock

import openai
import pytest
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


@pytest.fixture
def mock_chat_client(monkeypatch):
    """Patch :class:`radis.chats.utils.chat_client.ChatClient` so no real LLM call is made.

    Returns a :class:`unittest.mock.MagicMock` bound to ``ChatClient.extract_data``.
    Default behavior: every Schema field is populated with ``"YES"`` — convenient
    for YES/NO/MAYBE labeling schemas.

    Customize per-test by setting ``side_effect`` (the typical case for varying
    responses) or ``return_value``::

        def test_xyz(mock_chat_client):
            mock_chat_client.side_effect = (
                lambda prompt, Schema: Schema(lungs_clear="MAYBE")
            )
            label_report(report.id)
            assert mock_chat_client.call_count == 1

    Inspect call history via the standard :class:`MagicMock` attributes
    (``call_args_list``, ``call_args``, ``call_count``).

    The patch targets ``ChatClient`` itself, so any code that instantiates the
    class — including code running in ``ThreadPoolExecutor`` worker threads
    and in-process Procrastinate workers driven by ``run_worker_once`` — uses
    the mock. ``ChatClient.__init__`` is also neutered so no real OpenAI
    client is constructed (avoids needing a reachable LLM endpoint in tests).
    """
    from radis.chats.utils.chat_client import ChatClient

    extract_mock = MagicMock(name="ChatClient.extract_data")
    extract_mock.side_effect = lambda prompt, Schema: Schema(
        **{name: "YES" for name in Schema.model_fields}
    )

    monkeypatch.setattr(ChatClient, "__init__", lambda self: None)
    monkeypatch.setattr(
        ChatClient,
        "extract_data",
        lambda self, prompt, Schema: extract_mock(prompt, Schema),
    )

    return extract_mock
