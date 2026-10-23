import logging
from unittest.mock import AsyncMock, patch

import openai
import pytest

from radis.chats.utils.testing_helpers import make_connection_error
from radis.core.utils.llm_client import LLMResponseError
from radis.core.utils.model_spec import ModelSpec
from radis.core.utils.rate_limit import RateLimited
from radis.search.utils import description as writer

ANSWER = '"Kleiner apikaler Pneumothorax rechts. Kein Pleuraerguss."'
CLEANED = "Kleiner apikaler Pneumothorax rechts. Kein Pleuraerguss."


@pytest.fixture
def llm():
    with patch("radis.search.utils.description.AsyncChatClient") as client_cls:
        client = AsyncMock()
        client.chat.return_value = ANSWER
        client_cls.return_value = client
        yield client_cls, client


@pytest.mark.asyncio
async def test_returns_the_cleaned_passage_for_the_collapsed_terms(llm):
    result = await writer.write_description(" pneumothorax \n rechts ")

    assert result == CLEANED
    _, client = llm
    messages = client.chat.call_args.args[0]
    assert messages[0]["role"] == "system"
    assert "radiology reports" in messages[0]["content"]
    assert messages[1] == {"role": "user", "content": "pneumothorax rechts"}


@pytest.mark.asyncio
async def test_uses_the_query_generation_feature_with_its_own_budget(llm, settings):
    settings.SEARCH_DESCRIPTION_TIMEOUT = 7
    settings.SEARCH_DESCRIPTION_MAX_TOKENS = 50

    await writer.write_description("pneumothorax")

    client_cls, client = llm
    client_cls.assert_called_once_with("query_generation", timeout=7)
    assert client.chat.call_args.kwargs == {
        "max_completion_tokens": 50,
        "max_wait": 7.0,
        "transient_retry_attempts": 0,
    }
    client.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_answers_are_cached(llm):
    assert await writer.write_description("pneumothorax") == CLEANED
    assert await writer.write_description("pneumothorax") == CLEANED

    _, client = llm
    assert client.chat.call_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure",
    [make_connection_error(), RateLimited(), LLMResponseError("empty")],
    ids=["connection", "rate-limited", "no-content"],
)
async def test_failures_degrade_to_none_and_are_not_cached(llm, failure, caplog):
    _, client = llm
    client.chat.side_effect = [failure, ANSWER]

    with caplog.at_level(logging.WARNING, logger="radis.search.utils.description"):
        first = await writer.write_description("pneumothorax")
    second = await writer.write_description("pneumothorax")

    assert first is None
    assert second == CLEANED
    assert client.chat.call_count == 2
    assert "description writer failed" in caplog.text
    assert client.close.await_count == 2


@pytest.mark.asyncio
async def test_a_client_that_cannot_be_built_degrades_to_none(llm, caplog):
    """The OpenAI client refuses to construct without an API key."""
    client_cls, client = llm
    client_cls.side_effect = [openai.OpenAIError("Missing credentials"), client]

    with caplog.at_level(logging.WARNING, logger="radis.search.utils.description"):
        first = await writer.write_description("pneumothorax")
    second = await writer.write_description("pneumothorax")

    assert first is None
    assert "description writer failed: Missing credentials" in caplog.text
    # Nothing was cached, so the next request asks the LLM.
    assert second == CLEANED
    assert client.chat.call_count == 1
    client.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_blank_terms_never_call_the_llm(llm):
    assert await writer.write_description("   ") is None
    _, client = llm
    client.chat.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("blank", ['""', '" "'])
async def test_a_blank_answer_counts_as_failure(llm, blank):
    _, client = llm
    client.chat.side_effect = [blank, ANSWER]

    assert await writer.write_description("pneumothorax") is None
    assert await writer.write_description("pneumothorax") == CLEANED


@pytest.mark.asyncio
async def test_a_multi_line_answer_collapses_to_single_spaces(llm):
    _, client = llm
    client.chat.return_value = '  "Kleiner Pneumothorax.\n\n  Kein   Erguss."\n'

    assert await writer.write_description("pneumothorax") == "Kleiner Pneumothorax. Kein Erguss."


@pytest.mark.asyncio
async def test_the_cache_key_covers_the_prompt_and_the_model(llm, settings):
    _, client = llm
    await writer.write_description("pneumothorax")
    settings.SEARCH_DESCRIPTION_SYSTEM_PROMPT = "Another instruction."
    await writer.write_description("pneumothorax")
    settings.LLM_MODELS = {"query_generation": ModelSpec("other-model", {})}
    await writer.write_description("pneumothorax")

    assert client.chat.call_count == 3


@pytest.mark.asyncio
async def test_terms_share_a_cache_entry_by_their_collapsed_text(llm):
    _, client = llm
    client.chat.side_effect = ["First passage.", "Second passage."]

    assert await writer.write_description("pneumothorax  rechts") == "First passage."
    assert await writer.write_description(" pneumothorax\nrechts ") == "First passage."
    assert await writer.write_description("pneumothorax links") == "Second passage."

    assert client.chat.call_count == 2


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("LLM_BASE_URL", "http://another-endpoint/v1"),
        ("LLM_MODELS", {"query_generation": ModelSpec("model-b", {"temperature": 0})}),
        ("LLM_MODELS", {"query_generation": ModelSpec("model-a", {"temperature": 1})}),
    ],
    ids=["endpoint", "model-name", "model-parameters"],
)
async def test_each_model_setting_changes_the_cache_key(llm, settings, name, value):
    settings.LLM_MODELS = {"query_generation": ModelSpec("model-a", {"temperature": 0})}
    _, client = llm

    await writer.write_description("pneumothorax")
    setattr(settings, name, value)
    await writer.write_description("pneumothorax")

    assert client.chat.call_count == 2


@pytest.mark.asyncio
async def test_a_zero_cache_lifetime_disables_the_cache(llm, settings):
    settings.SEARCH_DESCRIPTION_CACHE_TIMEOUT_SECONDS = 0

    await writer.write_description("pneumothorax")
    await writer.write_description("pneumothorax")

    _, client = llm
    assert client.chat.call_count == 2
