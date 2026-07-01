import logging
from collections.abc import Iterable

import openai
from django.conf import settings
from openai.types.chat import ChatCompletionMessageParam
from pydantic import BaseModel

from radis.core.utils.rate_limit import (
    RateLimitGate,
    RpmLimiter,
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
    fallback_max_seconds=settings.LLM_RATE_LIMIT_FALLBACK_MAX_SECONDS,
    header_ceiling_seconds=settings.LLM_RATE_LIMIT_HEADER_CEILING_SECONDS,
)

# Process-global proactive cap on LLM requests/minute. Disabled when LLM_MAX_RPM <= 0.
_LLM_RPM_LIMITER = RpmLimiter(settings.LLM_MAX_RPM)


def _get_base_url() -> str:
    base_url = settings.EXTERNAL_LLM_PROVIDER_URL
    if not base_url:
        base_url = settings.LLM_SERVICE_URL
    return base_url


class AsyncChatClient:
    def __init__(self) -> None:
        base_url = _get_base_url()
        api_key = settings.EXTERNAL_LLM_PROVIDER_API_KEY
        # max_retries=0 so the gate fully owns backoff (no hidden SDK retries).
        self._client = openai.AsyncOpenAI(
            base_url=base_url,
            api_key=api_key,
            max_retries=0,
            timeout=settings.LLM_REQUEST_TIMEOUT_SECONDS,
        )
        self._model_name = settings.LLM_MODEL_NAME

    async def chat(
        self,
        messages: Iterable[ChatCompletionMessageParam],
        max_completion_tokens: int | None = None,
        max_wait: float | None = None,
    ) -> str:
        if max_wait is None:
            max_wait = float(settings.LLM_RATE_LIMIT_INTERACTIVE_MAX_WAIT_SECONDS)

        return await run_through_gate_async(
            _LLM_GATE,
            max_wait,
            lambda: with_transient_retries_async(
                lambda: self._chat(messages, max_completion_tokens),
                settings.LLM_TRANSIENT_RETRY_ATTEMPTS,
                settings.LLM_TRANSIENT_RETRY_BASE_SECONDS,
            ),
            rpm=_LLM_RPM_LIMITER,
        )

    async def _chat(
        self,
        messages: Iterable[ChatCompletionMessageParam],
        max_completion_tokens: int | None,
    ) -> str:
        logger.debug(f"Sending messages to LLM for chat:\n{messages}")
        request = {"model": self._model_name, "messages": messages}
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
    def __init__(self) -> None:
        base_url = _get_base_url()
        api_key = settings.EXTERNAL_LLM_PROVIDER_API_KEY
        # max_retries=0 so the gate fully owns backoff (no hidden SDK retries).
        self._client = openai.OpenAI(
            base_url=base_url,
            api_key=api_key,
            max_retries=0,
            timeout=settings.LLM_REQUEST_TIMEOUT_SECONDS,
        )
        self._llm_model_name = settings.LLM_MODEL_NAME
        # Provider quirks (e.g. Qwen's enable_thinking flag) sent with each call.
        self._extra_body: dict = getattr(settings, "LLM_EXTRA_BODY", {}) or {}

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
            rpm=_LLM_RPM_LIMITER,
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
