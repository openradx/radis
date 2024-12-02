import logging
from string import Template
from typing import Iterable

import openai
from django.conf import settings
from openai.types.chat import (ChatCompletionMessageParam,
                               ChatCompletionSystemMessageParam,
                               ChatCompletionUserMessageParam)

from radis.chats.models import Grammar

logger = logging.getLogger(__name__)


class AsyncChatClient:
    def __init__(self):
        self._client = openai.AsyncOpenAI(
            base_url=f"{settings.LLAMACPP_URL}/v1", api_key="unnecessary"
        )

    async def send_messages(
        self,
        messages: Iterable[ChatCompletionMessageParam],
        grammar: Grammar,
        max_tokens: int | None = None,
    ) -> str:
        messages = [
            ChatCompletionSystemMessageParam(
                role="system", content=settings.CHAT_GENERAL_SYSTEM_PROMPT
            ),
            *messages,
        ]

        logger.debug(f"Sending messages to LLM:\n{messages}")
        logger.debug(f"Using grammar: {grammar.human_readable_name}")

        completion = await self._client.chat.completions.create(
            model="option_for_local_llm_not_needed",
            messages=messages,
            max_tokens=max_tokens,
            extra_body={"grammar": grammar.grammar},
        )

        answer = completion.choices[0].message.content
        assert answer is not None
        logger.debug("Received from LLM: %s", answer)

        return answer

    async def ask_question(
        self,
        question: str,
        grammar: Grammar,
    ) -> str:
        system_prompt_str = Template(settings.CHAT_QUESTION_SYSTEM_PROMPT).substitute(
            {"grammar_instructions": grammar.llm_instruction}
        )
        user_prompt_str = Template(settings.CHAT_QUESTION_USER_PROMPT).substitute(
            {"question": question}
        )

        answer = await self.send_messages(
            [
                ChatCompletionSystemMessageParam(role="system", content=system_prompt_str),
                ChatCompletionUserMessageParam(role="user", content=user_prompt_str),
            ],
            grammar=grammar,
        )

        return answer

    async def ask_report_question(
        self,
        context: str,
        question: str,
        grammar: Grammar,
    ) -> str:
        system_prompt = Template(settings.CHAT_REPORT_QUESTION_SYSTEM_PROMPT).substitute(
            grammar_instructions=grammar.llm_instruction, report=context
        )
        user_prompt = Template(settings.CHAT_REPORT_QUESTION_USER_PROMPT).substitute(
            {"question": question}
        )

        answer = await self.send_messages(
            [
                ChatCompletionSystemMessageParam(role="system", content=system_prompt),
                ChatCompletionUserMessageParam(role="user", content=user_prompt),
            ],
            grammar=grammar,
        )

        return answer
