"""Tests for the ChatClient timeout + parse-handling contract (HIGH #3 fix).

The two classes here pin behavior that was previously absent — the older
client used the OpenAI client's 10-minute default timeout (so a hung
upstream pinned a worker for 10 minutes) and used bare ``assert`` on the
parsed result (so a model refusal surfaced as ``AssertionError("")``
which lands upstream as an empty LabelingRun.error_message).

If a future change drops the timeout argument, swallows refusals, or
re-introduces bare asserts on the parse result, these tests fail.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from pydantic import BaseModel


class _DummySchema(BaseModel):
    answer: str


def _make_completion(
    *, parsed=None, refusal=None, content=None
) -> SimpleNamespace:
    """Build a stand-in for an OpenAI completion response. The real client
    returns a Pydantic model; we mimic only the attributes ChatClient reads.
    """
    message = SimpleNamespace(parsed=parsed, refusal=refusal, content=content)
    choice = SimpleNamespace(message=message)
    return SimpleNamespace(choices=[choice])


# -- Timeout wiring --


class TestChatClientTimeout:
    """The timeout is the load-bearing piece of HIGH #3. Without it, the
    OpenAI client defaults to 10 minutes; any hung upstream pins a worker
    thread for 10 minutes before procrastinate can reassign the batch.
    """

    def test_chatclient_constructor_passes_timeout_from_settings(self, settings):
        from radis.chats.utils.chat_client import ChatClient

        settings.LLM_REQUEST_TIMEOUT_SECONDS = 17.5
        with patch("radis.chats.utils.chat_client.openai.OpenAI") as mock_openai:
            ChatClient()

        mock_openai.assert_called_once()
        passed_timeout = mock_openai.call_args.kwargs.get("timeout")
        assert passed_timeout == 17.5

    def test_chatclient_falls_back_to_default_when_setting_absent(self, settings):
        """If the setting was never declared, fall back to 60s rather than
        the OpenAI default of 600s. The fallback is the safety net that
        prevents accidental regression if the setting is removed.
        """
        from radis.chats.utils.chat_client import ChatClient

        # delattr() ensures getattr() takes the fallback branch.
        delattr(settings, "LLM_REQUEST_TIMEOUT_SECONDS")
        with patch("radis.chats.utils.chat_client.openai.OpenAI") as mock_openai:
            ChatClient()

        passed_timeout = mock_openai.call_args.kwargs.get("timeout")
        assert passed_timeout == 60.0

    def test_async_chatclient_passes_timeout_too(self, settings):
        from radis.chats.utils.chat_client import AsyncChatClient

        settings.LLM_REQUEST_TIMEOUT_SECONDS = 22.0
        with patch("radis.chats.utils.chat_client.openai.AsyncOpenAI") as mock_async:
            AsyncChatClient()

        passed_timeout = mock_async.call_args.kwargs.get("timeout")
        assert passed_timeout == 22.0


# -- extract_data error paths --


class TestExtractDataErrorPaths:
    """The structured-output call has two failure modes that used to surface
    as ``AssertionError("")`` and undiagnosable upstream FAILURE rows.
    """

    def _build_client_with_response(self, completion) -> "object":
        """Construct a ChatClient whose internal OpenAI client is mocked to
        return the given completion on the structured-parse call.
        """
        from radis.chats.utils.chat_client import ChatClient

        with patch("radis.chats.utils.chat_client.openai.OpenAI") as mock_openai:
            mock_client = MagicMock()
            mock_client.beta.chat.completions.parse.return_value = completion
            mock_openai.return_value = mock_client
            client = ChatClient()
        return client

    def test_refusal_raises_descriptive_runtime_error(self):
        client = self._build_client_with_response(
            _make_completion(refusal="Cannot answer questions about that.")
        )

        with pytest.raises(RuntimeError, match=r"refused.*Cannot answer"):
            client.extract_data("anything", _DummySchema)

    def test_parsed_none_raises_with_raw_content_in_message(self):
        """The error message must include the raw content so an investigator
        can tell whether the model returned malformed JSON, off-topic text,
        or nothing at all.
        """
        client = self._build_client_with_response(
            _make_completion(parsed=None, content="not json at all")
        )

        with pytest.raises(RuntimeError, match=r"no parseable.*not json at all"):
            client.extract_data("anything", _DummySchema)

    def test_parsed_none_with_no_content_still_raises(self):
        """Empty raw content is the worst case — model returned nothing
        useful. We still need a descriptive error, not an empty string.
        """
        client = self._build_client_with_response(
            _make_completion(parsed=None, content=None)
        )

        with pytest.raises(RuntimeError, match=r"no parseable"):
            client.extract_data("anything", _DummySchema)

    def test_successful_parsed_object_is_returned(self):
        """Regression guard: the happy path must still work after the
        refusal/None handling additions.
        """
        parsed = _DummySchema(answer="ok")
        client = self._build_client_with_response(
            _make_completion(parsed=parsed)
        )

        result = client.extract_data("anything", _DummySchema)

        assert result is parsed


# -- complete_text error paths --


class TestCompleteTextErrorPaths:
    """``complete_text`` had the same shape of bug — ``assert content is not
    None`` raised ``AssertionError("")`` on empty content. The empty-content
    case is the load-bearing one for Qwen REASONED mode: if LLM_EXTRA_BODY
    isn't forwarded, the response.content is empty (CoT got routed to a
    separate field), and we want a descriptive error pointing at the cause.
    """

    def _build_client_with_response(self, completion):
        from radis.chats.utils.chat_client import ChatClient

        with patch("radis.chats.utils.chat_client.openai.OpenAI") as mock_openai:
            mock_client = MagicMock()
            mock_client.chat.completions.create.return_value = completion
            mock_openai.return_value = mock_client
            client = ChatClient()
        return client

    def test_refusal_raises_descriptive_error(self):
        client = self._build_client_with_response(
            _make_completion(refusal="Policy.", content=None)
        )

        with pytest.raises(RuntimeError, match=r"refused.*Policy"):
            client.complete_text("anything")

    def test_empty_content_raises_with_qwen_hint(self):
        """The error message includes a hint about LLM_EXTRA_BODY because
        that is the most common cause of this failure mode in our setup.
        """
        client = self._build_client_with_response(
            _make_completion(content="")
        )

        with pytest.raises(RuntimeError, match=r"empty content.*LLM_EXTRA_BODY"):
            client.complete_text("anything")

    def test_none_content_raises(self):
        client = self._build_client_with_response(
            _make_completion(content=None)
        )

        with pytest.raises(RuntimeError, match=r"empty content"):
            client.complete_text("anything")

    def test_successful_content_is_returned(self):
        """Regression guard for the happy path."""
        client = self._build_client_with_response(
            _make_completion(content="hello world")
        )

        result = client.complete_text("anything")

        assert result == "hello world"
