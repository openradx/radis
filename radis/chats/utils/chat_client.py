import logging
from typing import Iterable

import instructor
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
        self._client = openai.AsyncOpenAI(base_url=base_url, api_key=api_key)
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

        client = openai.OpenAI(base_url=base_url, api_key=api_key)
        self._client = instructor.from_openai(client)
        self._llm_model_name = settings.LLM_MODEL_NAME

    def extract_data(self, prompt: str, schema: type[BaseModel]) -> BaseModel:
        logger.debug("Sending prompt and schema to LLM to extract data.")
        logger.debug("Prompt:\n%s", prompt)
        logger.debug("Schema:\n%s", schema.model_json_schema())

        result = self._client.chat.completions.create(
            model=self._llm_model_name,
            messages=[{"role": "system", "content": prompt}],
            response_model=schema,
        )
        logger.debug("Received from LLM: %s", result)
        return result
