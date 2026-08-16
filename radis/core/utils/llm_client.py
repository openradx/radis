import logging
from collections.abc import Iterable

import openai
from django.conf import settings
from openai.types.chat import ChatCompletionMessageParam
from pydantic import BaseModel

from radis.core.utils.model_spec import ModelSpec
from radis.core.utils.rate_limit import (
    RateLimitGate,
    run_through_gate,
    run_through_gate_async,
    with_transient_retries,
    with_transient_retries_async,
)

logger = logging.getLogger(__name__)


class LLMResponseError(Exception):
    """The LLM returned no usable content (e.g. a refusal or an empty completion)."""


# Process-global so every LLM caller in this worker/web process shares one backoff window.
_LLM_GATE = RateLimitGate(
    base_seconds=settings.LLM_RATE_LIMIT_BACKOFF_BASE_SECONDS,
    backoff_max_seconds=settings.LLM_RATE_LIMIT_BACKOFF_MAX_SECONDS,
    header_ceiling_seconds=settings.LLM_RATE_LIMIT_HEADER_CEILING_SECONDS,
)


def _model_spec(feature: str) -> ModelSpec:
    """The model and request parameters configured for a feature.

    Unknown features are a programming error rather than a configuration one: the
    feature names come from LLM_FEATURES, not from user input.
    """
    try:
        return settings.LLM_MODELS[feature]
    except KeyError:
        raise ValueError(
            f"Unknown LLM feature {feature!r}, expected one of {sorted(settings.LLM_MODELS)}"
        ) from None


class AsyncChatClient:
    def __init__(self, feature: str, *, timeout: float | None = None) -> None:
        """`feature` selects the configured model (see LLM_FEATURES).

        `timeout` overrides the per-request HTTP timeout (interactive callers
        may want a much shorter one than the worker default).
        """
        # max_retries=0 so the gate fully owns backoff (no hidden SDK retries).
        self._client = openai.AsyncOpenAI(
            base_url=settings.LLM_BASE_URL,
            api_key=settings.LLM_API_KEY,
            max_retries=0,
            timeout=timeout if timeout is not None else settings.LLM_REQUEST_TIMEOUT_SECONDS,
        )
        spec = _model_spec(feature)
        self._model_name = spec.model
        self._extra_body = spec.params

    async def close(self) -> None:
        """Close the underlying HTTP client; call once the client is no longer needed."""
        await self._client.close()

    async def chat(
        self,
        messages: Iterable[ChatCompletionMessageParam],
        max_completion_tokens: int | None = None,
        max_wait: float | None = None,
        transient_retry_attempts: int | None = None,
    ) -> str:
        if max_wait is None:
            max_wait = float(settings.LLM_RATE_LIMIT_INTERACTIVE_MAX_WAIT_SECONDS)
        attempts: int = (
            transient_retry_attempts
            if transient_retry_attempts is not None
            else settings.LLM_TRANSIENT_RETRY_ATTEMPTS
        )

        return await run_through_gate_async(
            _LLM_GATE,
            max_wait,
            lambda: with_transient_retries_async(
                lambda: self._chat(messages, max_completion_tokens),
                attempts,
                settings.LLM_TRANSIENT_RETRY_BASE_SECONDS,
            ),
        )

    async def _chat(
        self,
        messages: Iterable[ChatCompletionMessageParam],
        max_completion_tokens: int | None,
    ) -> str:
        logger.debug(f"Sending messages to LLM for chat:\n{messages}")
        request = {
            "model": self._model_name,
            "messages": messages,
            "extra_body": self._extra_body,
        }
        if max_completion_tokens is not None:
            request["max_completion_tokens"] = max_completion_tokens

        completion = await self._client.chat.completions.create(**request)
        answer = completion.choices[0].message.content
        if answer is None:  # a refusal or empty completion, not a programmer invariant
            raise LLMResponseError(
                f"LLM returned no content (model={self._model_name}, "
                f"finish_reason={completion.choices[0].finish_reason})"
            )
        logger.debug("Received from LLM: %s", answer)
        return answer


class LLMClient:
    def __init__(self, feature: str) -> None:
        """`feature` selects the configured model (see LLM_FEATURES)."""
        # max_retries=0 so the gate fully owns backoff (no hidden SDK retries).
        self._client = openai.OpenAI(
            base_url=settings.LLM_BASE_URL,
            api_key=settings.LLM_API_KEY,
            max_retries=0,
            timeout=settings.LLM_REQUEST_TIMEOUT_SECONDS,
        )
        spec = _model_spec(feature)
        self._llm_model_name = spec.model
        # Request parameters configured with the model, e.g. reasoning_effort.
        self._extra_body = spec.params

    def extract_data(
        self,
        prompt: str,
        schema: type[BaseModel],
        max_wait: float | None = None,
    ) -> BaseModel:
        if max_wait is None:
            max_wait = float(settings.LLM_RATE_LIMIT_MAX_WAIT_SECONDS)

        return run_through_gate(
            _LLM_GATE,
            max_wait,
            lambda: with_transient_retries(
                lambda: self._extract_data(prompt, schema),
                settings.LLM_TRANSIENT_RETRY_ATTEMPTS,
                settings.LLM_TRANSIENT_RETRY_BASE_SECONDS,
            ),
        )

    def _extract_data(self, prompt: str, schema: type[BaseModel]) -> BaseModel:
        logger.debug("Sending prompt and schema to LLM to extract data.")
        logger.debug("Prompt:\n%s", prompt)
        logger.debug("Schema:\n%s", schema.model_json_schema())

        completion = self._client.beta.chat.completions.parse(
            model=self._llm_model_name,
            messages=[{"role": "user", "content": prompt}],
            response_format=schema,
            extra_body=self._extra_body,
        )
        event = completion.choices[0].message.parsed
        if event is None:  # a refusal or a parse failure, not a programmer invariant
            raise LLMResponseError(
                f"LLM returned no parsed response (model={self._llm_model_name}, "
                f"finish_reason={completion.choices[0].finish_reason})"
            )
        logger.debug("Received from LLM: %s", event)
        return event
