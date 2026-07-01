from typing import cast
from unittest.mock import MagicMock, patch

import pytest
from pydantic import BaseModel

from radis.chats.utils.testing_helpers import (
    create_async_openai_client_mock,
    create_openai_client_mock,
    make_rate_limit_error,
)
from radis.core.utils import llm_client
from radis.core.utils.llm_client import (
    _LLM_GATE,
    _LLM_RPM_LIMITER,
    AsyncChatClient,
    LLMClient,
    LLMResponseError,
)
from radis.core.utils.rate_limit import RateLimited, RpmLimiter


class _Schema(BaseModel):
    value: str


@pytest.fixture(autouse=True)
def reset_gate():
    _LLM_GATE.reset()
    _LLM_RPM_LIMITER.reset()
    yield
    _LLM_GATE.reset()
    _LLM_RPM_LIMITER.reset()


def test_llm_client_sets_max_retries_and_timeout(settings):
    settings.LLM_REQUEST_TIMEOUT_SECONDS = 42.0
    with patch("openai.OpenAI") as openai_cls:
        LLMClient()
    kwargs = openai_cls.call_args.kwargs
    assert kwargs["max_retries"] == 0
    assert kwargs["timeout"] == 42.0


def test_extract_data_uses_user_message_and_extra_body(settings):
    settings.LLM_EXTRA_BODY = {"foo": "bar"}
    mock = cast(MagicMock, create_openai_client_mock(_Schema(value="hi")))
    with patch("openai.OpenAI", return_value=mock):
        result = LLMClient().extract_data("the prompt", _Schema)
    assert isinstance(result, _Schema)
    call = mock.beta.chat.completions.parse.call_args.kwargs
    assert call["messages"] == [{"role": "user", "content": "the prompt"}]
    assert call["extra_body"] == {"foo": "bar"}


def test_extract_data_recovers_after_one_rate_limit():
    # No real wait: the injected 429 carries retry-after: 0. (`_LLM_GATE` reads its backoff
    # settings once at import time, so overriding them via `settings` here would be a no-op.)
    mock = cast(MagicMock, create_openai_client_mock(_Schema(value="ok")))
    calls = {"n": 0}
    success_response = mock.beta.chat.completions.parse.return_value

    def flaky(**kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise make_rate_limit_error({"retry-after": "0"})
        return success_response

    mock.beta.chat.completions.parse.side_effect = flaky
    with patch("openai.OpenAI", return_value=mock):
        result = LLMClient().extract_data("p", _Schema)
    assert isinstance(result, _Schema)
    assert result.value == "ok"
    assert calls["n"] == 2


def test_extract_data_defers_when_rate_limit_exceeds_budget():
    mock = cast(MagicMock, create_openai_client_mock(_Schema(value="never")))

    def always_429(**kwargs):
        raise make_rate_limit_error({"retry-after": "600"})

    mock.beta.chat.completions.parse.side_effect = always_429
    with patch("openai.OpenAI", return_value=mock):
        with pytest.raises(RateLimited):
            LLMClient().extract_data("p", _Schema, max_wait=300.0)


@pytest.mark.asyncio
async def test_chat_returns_content():
    mock = create_async_openai_client_mock("the answer")
    with patch("openai.AsyncOpenAI", return_value=mock):
        answer = await AsyncChatClient().chat([{"role": "user", "content": "hi"}])
    assert answer == "the answer"


@pytest.mark.asyncio
async def test_chat_defers_when_rate_limit_exceeds_budget():
    mock = cast(MagicMock, create_async_openai_client_mock("never"))

    async def always_429(**kwargs):
        raise make_rate_limit_error({"retry-after": "600"})

    mock.chat.completions.create.side_effect = always_429
    with patch("openai.AsyncOpenAI", return_value=mock):
        with pytest.raises(RateLimited):
            await AsyncChatClient().chat([{"role": "user", "content": "hi"}], max_wait=20.0)


@pytest.mark.asyncio
async def test_chat_raises_when_content_is_none():
    # A refusal/empty completion yields content=None; surface it explicitly instead of
    # asserting (which would be stripped under python -O).
    mock = create_async_openai_client_mock(None)
    with patch("openai.AsyncOpenAI", return_value=mock):
        with pytest.raises(LLMResponseError):
            await AsyncChatClient().chat([{"role": "user", "content": "hi"}])


def test_extract_data_raises_when_parsed_is_none():
    mock = create_openai_client_mock(None)
    with patch("openai.OpenAI", return_value=mock):
        with pytest.raises(LLMResponseError):
            LLMClient().extract_data("p", _Schema)


def test_default_rpm_limiter_is_disabled():
    # Default LLM_MAX_RPM is 0, so the process-global limiter is a no-op.
    assert _LLM_RPM_LIMITER.acquire(deadline=0.0) is True


def test_extract_data_defers_when_rpm_exhausted(monkeypatch):
    # A drained 1-rpm limiter: the next permit is 60s away, beyond a 30s budget,
    # so the limiter (reached via the client) defers without ever calling the LLM.
    limiter = RpmLimiter(1)
    assert limiter.acquire(deadline=0.0) is True  # consumes the starting permit (no wait)
    monkeypatch.setattr(llm_client, "_LLM_RPM_LIMITER", limiter)

    mock = create_openai_client_mock(_Schema(value="never"))
    with patch("openai.OpenAI", return_value=mock):
        with pytest.raises(RateLimited):
            LLMClient().extract_data("p", _Schema, max_wait=30.0)
    cast(MagicMock, mock).beta.chat.completions.parse.assert_not_called()


@pytest.mark.asyncio
async def test_chat_defers_when_rpm_exhausted(monkeypatch):
    limiter = RpmLimiter(1)
    assert limiter.acquire(deadline=0.0) is True  # consumes the starting permit (no wait)
    monkeypatch.setattr(llm_client, "_LLM_RPM_LIMITER", limiter)

    mock = create_async_openai_client_mock("never")
    with patch("openai.AsyncOpenAI", return_value=mock):
        with pytest.raises(RateLimited):
            await AsyncChatClient().chat([{"role": "user", "content": "hi"}], max_wait=30.0)
    cast(MagicMock, mock).chat.completions.create.assert_not_called()
