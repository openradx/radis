import logging
from typing import Iterable

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


def _validate_completion_response(completion) -> str:
    """
    Validates that the LLM completion response contains valid content.

    Args:
        completion: The completion response from the LLM

    Returns:
        The message content as a string

    Raises:
        ValueError: If the response is empty or invalid
    """
    if not completion.choices:
        logger.error("LLM returned empty choices list")
        raise ValueError("LLM returned no response choices")

    answer = completion.choices[0].message.content
    if answer is None:
        logger.error("LLM returned None for message content")
        raise ValueError("LLM returned empty response content")

    return answer


def _validate_parsed_response(completion) -> BaseModel:
    """
    Validates that the LLM completion response contains valid parsed data.

    Args:
        completion: The completion response from the LLM

    Returns:
        The parsed BaseModel instance

    Raises:
        ValueError: If the response is empty or invalid
    """
    if not completion.choices:
        logger.error("LLM returned empty choices list")
        raise ValueError("LLM returned no response choices")

    parsed = completion.choices[0].message.parsed
    if parsed is None:
        logger.error("LLM returned None for parsed message")
        raise ValueError("LLM returned empty parsed response")

    return parsed


def _handle_api_error(error: openai.APIError, operation: str) -> None:
    """
    Logs and re-raises API errors with consistent error messages.

    Args:
        error: The API error from OpenAI
        operation: Description of the operation that failed (e.g., "chat", "data extraction")

    Raises:
        RuntimeError: Always raises with a user-friendly error message
    """
    logger.error(f"OpenAI API error during {operation}: {error}")
    raise RuntimeError(f"Failed to communicate with LLM service: {error}") from error


class _BaseChatClient:
    """Base class containing shared chat client logic."""

    def __init__(self):
        self._base_url = _get_base_url()
        self._api_key = settings.EXTERNAL_LLM_PROVIDER_API_KEY
        self._model_name = settings.LLM_MODEL_NAME

    def _build_chat_request(
        self,
        messages: Iterable[ChatCompletionMessageParam],
        max_completion_tokens: int | None = None,
    ) -> dict:
        """Build the request dictionary for chat completion."""
        request = {
            "model": self._model_name,
            "messages": messages,
        }
        if max_completion_tokens is not None:
            request["max_completion_tokens"] = max_completion_tokens
        return request

    def _log_request(self, messages: Iterable[ChatCompletionMessageParam]) -> None:
        """Log the outgoing request."""
        logger.debug(f"Sending messages to LLM for chat:\n{messages}")

    def _log_response(self, answer: str) -> None:
        """Log the response from LLM."""
        logger.debug("Received from LLM: %s", answer)


class AsyncChatClient(_BaseChatClient):
    def __init__(self):
        super().__init__()
        self._client = openai.AsyncOpenAI(base_url=self._base_url, api_key=self._api_key)

    async def chat(
        self,
        messages: Iterable[ChatCompletionMessageParam],
        max_completion_tokens: int | None = None,
    ) -> str:
        self._log_request(messages)
        request = self._build_chat_request(messages, max_completion_tokens)

        try:
            completion = await self._client.chat.completions.create(**request)
        except openai.APIError as e:
            _handle_api_error(e, "chat")

        answer = _validate_completion_response(completion)
        self._log_response(answer)
        return answer


class ChatClient(_BaseChatClient):
    def __init__(self) -> None:
        super().__init__()
        self._client = openai.OpenAI(base_url=self._base_url, api_key=self._api_key)

    def chat(
        self,
        messages: Iterable[ChatCompletionMessageParam],
        max_completion_tokens: int | None = None,
    ) -> str:
        """
        Send messages to LLM and return the response text.

        Args:
            messages: List of message dictionaries with 'role' and 'content'
            max_completion_tokens: Optional maximum tokens to generate

        Returns:
            The LLM's response as a string
        """
        self._log_request(messages)
        request = self._build_chat_request(messages, max_completion_tokens)

        try:
            completion = self._client.chat.completions.create(**request)
        except openai.APIError as e:
            _handle_api_error(e, "chat")

        answer = _validate_completion_response(completion)
        self._log_response(answer)
        return answer

    def extract_data(self, prompt: str, schema: type[BaseModel]) -> BaseModel:
        logger.debug("Sending prompt and schema to LLM to extract data.")
        logger.debug("Prompt:\n%s", prompt)
        logger.debug("Schema:\n%s", schema.model_json_schema())

        try:
            completion = self._client.beta.chat.completions.parse(
                model=self._model_name,
                messages=[{"role": "system", "content": prompt}],
                response_format=schema,
            )
        except openai.APIError as e:
            _handle_api_error(e, "data extraction")

        event = _validate_parsed_response(completion)
        logger.debug("Received from LLM: %s", event)
        return event
