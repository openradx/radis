from random import randint
from time import sleep
from typing import Any, Literal

import openai
from openai import OpenAI
from openai.types.chat import ChatCompletionMessageParam

SYSTEM_PROMPT = {
    "de": "Du bist ein Radiologe.",
    "en": "You are a radiologist.",
}

USER_PROMPT = {
    "de": "Schreibe einen radiologischen Befund.",
    "en": "Write a radiology report.",
}


class ReportGenerator:
    def __init__(
        self,
        api_key: str,
        model: str = "gpt-3.5-turbo",
        max_tokens: int = 4096,
        language: Literal["en", "de"] = "en",
    ) -> None:
        self._client = OpenAI(api_key=api_key)
        self._model = model
        self._max_tokens = max_tokens
        self._language = language

        self.messages: list[ChatCompletionMessageParam] = [
            {"role": "system", "content": SYSTEM_PROMPT[self._language]},
            {"role": "user", "content": USER_PROMPT[self._language]},
        ]

    def generate_report(self) -> str:
        response: Any = None
        retries = 0
        while not response:
            try:
                response = self._client.chat.completions.create(
                    messages=self.messages,
                    model=self._model,
                )
            # For available errors see https://github.com/openai/openai-python#handling-errors
            except openai.APIStatusError as err:
                retries += 1
                if retries == 3:
                    print(f"Error! Service unavailable even after 3 retries: {err}")
                    raise err

                # maybe use rate limiter like https://github.com/tomasbasham/ratelimit
                sleep(randint(1, 5))

        return response.choices[0].message.content
