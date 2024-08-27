import logging
from string import Template
from typing import Iterable, Literal

import openai
from django.conf import settings
from openai.types.chat import ChatCompletionMessageParam

logger = logging.getLogger(__name__)


class AsyncChatClient:
    def __init__(self):
        self._client = openai.AsyncOpenAI(
            base_url=f"{settings.LLAMACPP_URL}/v1", api_key="unnecessary"
        )

    async def send_messages(
        self,
        messages: Iterable[ChatCompletionMessageParam],
        max_tokens: int | None = None,
        grammar: str = "",
    ) -> str:
        log_msg = f"Sending messages to LLM:\n{messages}"
        if grammar:
            log_msg += f"\nUsing grammar: {grammar}"
        logger.debug(log_msg)

        completion = await self._client.chat.completions.create(
            model="unnecessary",
            messages=messages,
            max_tokens=max_tokens,
            extra_body={"grammar": grammar},
        )
        answer = completion.choices[0].message.content
        assert answer is not None
        logger.debug("Received from LLM: %s", answer)

        return answer

    async def ask_report_question(self, context: str, question: str) -> str:
        system_prompt = Template(settings.CHAT_REPORT_QUESTION_SYSTEM_PROMPT).substitute(
            {"report": context}
        )
        user_prompt = Template(settings.CHAT_REPORT_QUESTION_USER_PROMPT).substitute(
            {"question": question}
        )

        return await self.send_messages(
            [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
        )

    async def ask_report_yes_no_question(self, context: str, question: str) -> Literal["yes", "no"]:
        system_prompt = Template(settings.CHAT_REPORT_YES_NO_QUESTION_SYSTEM_PROMPT).substitute(
            {"report": context}
        )
        user_prompt = Template(settings.CHAT_REPORT_QUESTION_USER_PROMPT).substitute(
            {"question": question}
        )

        answer = await self.send_messages(
            [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            grammar=settings.CHAT_YES_NO_ANSWER_GRAMMAR,
        )

        if answer == "Yes":
            return "yes"
        elif answer == "No":
            return "no"
        else:
            raise ValueError(f"Unexpected answer: {answer}")
