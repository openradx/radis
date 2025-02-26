import logging
from string import Template
from typing import Iterable

import openai
from django.conf import settings
from openai.types.chat import ChatCompletionMessageParam
from pydantic import BaseModel

logger = logging.getLogger(__name__)


class AsyncChatClient:
    def __init__(self):
        self._client = openai.AsyncOpenAI(
            base_url=f"{settings.LLAMACPP_URL}/v1", api_key="no_api_key_needed"
        )

    async def chat(
        self,
        messages: Iterable[ChatCompletionMessageParam],
        max_tokens: int | None = None,
    ) -> str:
        logger.debug(f"Sending messages to LLM for chat:\n{messages}")

        completion = await self._client.chat.completions.create(
            model="no_model_needed",
            messages=messages,
            max_tokens=max_tokens or openai.NOT_GIVEN,
        )
        answer = completion.choices[0].message.content
        assert answer is not None
        logger.debug("Received from LLM: %s", answer)
        return answer

    async def chat_about_report(self, report: str, prompt: str) -> str:
        system_prompt = Template(settings.CHAT_REPORT_SYSTEM_PROMPT).substitute({"report": report})
        return await self.chat(
            [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": prompt},
            ]
        )


class ChatClient:
    def __init__(self) -> None:
        self._client = openai.OpenAI(
            base_url=f"{settings.LLAMACPP_URL}/v1", api_key="no_api_key_needed"
        )

    def extract_data(self, prompt: str, schema: type[BaseModel]) -> BaseModel:
        logger.debug("Sending prompt and schema to LLM to extract data.")
        logger.debug("Prompt:\n%s", prompt)
        logger.debug("Schema:\n%s", schema.model_json_schema())

        completion = self._client.beta.chat.completions.parse(
            model="no_model_needed",
            messages=[{"role": "system", "content": prompt}],
            response_format=schema,
        )
        event = completion.choices[0].message.parsed
        assert event
        logger.debug("Received from LLM: %s", event)
        return event
