import logging
from collections.abc import Iterable

import openai
from django.conf import settings
from openai.types.chat import ChatCompletionMessageParam
from pydantic import BaseModel

logger = logging.getLogger(__name__)


def _get_base_url() -> str:
    base_url = settings.EXTERNAL_LLM_PROVIDER_URL
    if not base_url:
        base_url = settings.LLM_SERVICE_URL
    return base_url


class AsyncChatClient:
    def __init__(self):
        base_url = _get_base_url()
        api_key = settings.EXTERNAL_LLM_PROVIDER_API_KEY
        # Use the configured per-request timeout so a hung upstream cannot
        # pin a worker for the OpenAI client's 10-minute default. See
        # settings.LLM_REQUEST_TIMEOUT_SECONDS for the rationale.
        timeout = getattr(settings, "LLM_REQUEST_TIMEOUT_SECONDS", 60.0)
        self._client = openai.AsyncOpenAI(
            base_url=base_url, api_key=api_key, timeout=timeout
        )
        self._model_name = settings.LLM_MODEL_NAME

    async def chat(
        self,
        messages: Iterable[ChatCompletionMessageParam],
        max_completion_tokens: int | None = None,
    ) -> str:
        logger.debug(f"Sending messages to LLM for chat:\n{messages}")

        request = {
            "model": self._model_name,
            "messages": messages,
        }
        if max_completion_tokens is not None:
            request["max_completion_tokens"] = max_completion_tokens

        completion = await self._client.chat.completions.create(**request)
        answer = completion.choices[0].message.content
        assert answer is not None
        logger.debug("Received from LLM: %s", answer)
        return answer


class ChatClient:
    def __init__(self) -> None:
        base_url = _get_base_url()
        api_key = settings.EXTERNAL_LLM_PROVIDER_API_KEY

        # Use the configured per-request timeout so a hung upstream cannot
        # pin a worker for the OpenAI client's 10-minute default. The label
        # processor runs threads against this client and depends on calls
        # failing fast under failure so the next batch can be picked up;
        # see settings.LLM_REQUEST_TIMEOUT_SECONDS for the full rationale.
        timeout = getattr(settings, "LLM_REQUEST_TIMEOUT_SECONDS", 60.0)
        self._client = openai.OpenAI(
            base_url=base_url, api_key=api_key, timeout=timeout
        )
        self._llm_model_name = settings.LLM_MODEL_NAME
        # Server-side extra body forwarded with every chat call. Provider
        # quirks (e.g. Qwen3.6's enable_thinking flag) belong here so they
        # apply uniformly to extract_data and complete_text without each
        # caller needing to remember them. Empty dict if unset.
        self._extra_body: dict = getattr(settings, "LLM_EXTRA_BODY", {}) or {}

    def extract_data(self, prompt: str, schema: type[BaseModel]) -> BaseModel:
        """Structured-output call: ask the LLM to fill the given Pydantic
        schema and return the parsed object.

        Failure modes worth being explicit about (HIGH #3 fix):

        * **Model refusal.** ``message.refusal`` is non-empty when the
          model decided to refuse (e.g. safety policy, malformed schema).
          The previous version's ``assert event`` raised
          ``AssertionError("")`` here, which surfaced upstream as a
          LabelingRun with ``error_message=""`` — undiagnosable. We now
          raise a descriptive RuntimeError that includes the refusal text.
        * **Unparseable output.** ``message.parsed is None`` happens when
          the model returned content that didn't validate against the
          schema (rare with strict Literal enums but possible if the
          backend silently ignored ``response_format``). We surface the
          raw content so an investigator can see what the model actually
          said.
        """
        logger.debug("Sending prompt and schema to LLM to extract data.")
        logger.debug("Prompt:\n%s", prompt)
        logger.debug("Schema:\n%s", schema.model_json_schema())

        kwargs: dict = {
            "model": self._llm_model_name,
            "messages": [{"role": "user", "content": prompt}],
            "response_format": schema,
        }
        if self._extra_body:
            kwargs["extra_body"] = self._extra_body
        completion = self._client.beta.chat.completions.parse(**kwargs)
        message = completion.choices[0].message

        refusal = getattr(message, "refusal", None)
        if refusal:
            raise RuntimeError(f"LLM refused to answer: {refusal}")

        if message.parsed is None:
            # Trim the raw content so a misbehaving model can't bloat
            # the error_message column past the column's reasonable size.
            raw = (message.content or "")[:1000]
            raise RuntimeError(
                f"LLM returned no parseable output for schema "
                f"{schema.__name__}. raw_content={raw!r}"
            )

        logger.debug("Received from LLM: %s", message.parsed)
        return message.parsed

    def complete_text(self, prompt: str) -> str:
        """Plain-text completion. Used by labelling's REASONED mode to elicit
        free-form chain-of-thought before the structured-output call. Schema
        constraints during reasoning tend to flatten useful CoT, so this is
        kept deliberately schema-free.

        Failure mode (HIGH #3 fix): ``message.content`` can be empty on
        provider refusal, on Qwen-without-extra-body (the routing-CoT
        case the LLM_EXTRA_BODY setting works around), or on hard
        downstream errors. The previous ``assert content is not None``
        raised ``AssertionError("")`` with no diagnostic value. We now
        raise descriptively.
        """
        logger.debug("Sending prompt to LLM for plain-text completion.")
        logger.debug("Prompt:\n%s", prompt)

        kwargs: dict = {
            "model": self._llm_model_name,
            "messages": [{"role": "user", "content": prompt}],
        }
        if self._extra_body:
            kwargs["extra_body"] = self._extra_body
        completion = self._client.chat.completions.create(**kwargs)
        message = completion.choices[0].message

        refusal = getattr(message, "refusal", None)
        if refusal:
            raise RuntimeError(f"LLM refused to answer: {refusal}")

        content = message.content
        if not content:
            raise RuntimeError(
                "LLM returned empty content for plain-text completion. "
                "Check provider configuration (e.g. LLM_EXTRA_BODY for "
                "Qwen-style separate-reasoning-field routing)."
            )

        logger.debug("Received from LLM: %s", content)
        return content
