import logging
from string import Template
from typing import Literal

import openai
from django.conf import settings

logger = logging.getLogger(__name__)

YES_NO_GRAMMAR = """
    root ::= Answer
    Answer ::= "$yes" | "$no"
"""


class ChatClient:
    def __init__(self):
        self._client = openai.OpenAI(base_url=f"{settings.LLAMACPP_URL}/v1", api_key="none")

    def ask_question(
        self, report_body: str, language: str, question: str, grammar: str | None = None
    ) -> str:
        system_prompt = settings.CHAT_SYSTEM_PROMPT[language]
        user_prompt = Template(settings.CHAT_USER_PROMPT[language]).substitute(
            {"report": report_body, "question": question}
        )

        log_msg = f"Sending to LLM:\n[System] {system_prompt}\n[User] {user_prompt}"
        if grammar:
            log_msg += f"\n[Grammar] {grammar}"
        logger.debug(log_msg)

        completion = self._client.chat.completions.create(
            model="none",
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            extra_body={"grammar": grammar},
        )

        answer = completion.choices[0].message.content
        assert answer is not None
        logger.debug("Received from LLM: %s", answer)

        return answer

    def ask_yes_no_question(
        self, report_body: str, language: str, question: str
    ) -> Literal["yes", "no"]:
        grammar = Template(YES_NO_GRAMMAR).substitute(
            {
                "yes": settings.CHAT_ANSWER_YES[language],
                "no": settings.CHAT_ANSWER_NO[language],
            }
        )

        llm_answer = self.ask_question(report_body, language, question, grammar)

        if llm_answer == settings.CHAT_ANSWER_YES[language]:
            return "yes"
        elif llm_answer == settings.CHAT_ANSWER_NO[language]:
            return "no"
        else:
            raise ValueError(f"Unexpected answer: {llm_answer}")


class AsyncChatClient:
    def __init__(self):
        self._client = openai.AsyncOpenAI(base_url=f"{settings.LLAMACPP_URL}/v1", api_key="none")

    async def ask_question(
        self, report_body: str, language: str, question: str, grammar: str | None = None
    ) -> str:
        system = settings.CHAT_SYSTEM_PROMPT[language]
        user_prompt = Template(settings.CHAT_USER_PROMPT[language]).substitute(
            {"report": report_body, "question": question}
        )

        log_msg = f"Sending to LLM:\n[System] {system}\n[User] {user_prompt}"
        if grammar:
            log_msg += f"\n[Grammar] {grammar}"
        logger.debug(log_msg)

        completion = await self._client.chat.completions.create(
            model="none",
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user_prompt},
            ],
            extra_body={"grammar": grammar},
        )

        answer = completion.choices[0].message.content
        assert answer is not None
        logger.debug("Received from LLM: %s", answer)

        return answer

    async def ask_yes_no_question(
        self, report_body: str, language: str, question: str
    ) -> Literal["yes", "no"]:
        grammar = Template(YES_NO_GRAMMAR).substitute(
            {
                "yes": settings.CHAT_ANSWER_YES[language],
                "no": settings.CHAT_ANSWER_NO[language],
            }
        )

        llm_answer = await self.ask_question(report_body, language, question, grammar)

        if llm_answer == settings.CHAT_ANSWER_YES[language]:
            return "yes"
        elif llm_answer == settings.CHAT_ANSWER_NO[language]:
            return "no"
        else:
            raise ValueError(f"Unexpected answer: {llm_answer}")
