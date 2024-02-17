from random import randint
from time import sleep
from typing import Any, Callable, Literal

import openai
import tiktoken
from openai import OpenAI
from openai.types.chat import ChatCompletionMessageParam

INITIAL_SYSTEM_PROMPT = {
    "de": "Du bist ein radiologischer Facharzt.",
    "en": "You are a senior radiologist.",
}

INITIAL_INSTRUCTION = {
    "de": "Schreibe einen radiologischen Befund als Beispiel.",
    "en": "Write an example radiology report.",
}

FOLLOWUP_INSTRUCTION = {
    "de": "Schreibe einen weiteren radiologischen Befund als Beispiel.",
    "en": "Write another example radiology report.",
}


def num_tokens_from_messages(
    messages: list[ChatCompletionMessageParam], model="gpt-3.5-turbo-0613", silent=False
):
    """Return the number of tokens used by a list of messages.

    From https://cookbook.openai.com/examples/how_to_count_tokens_with_tiktoken
    """
    try:
        encoding = tiktoken.encoding_for_model(model)
    except KeyError:
        if not silent:
            print("Warning: model not found. Using cl100k_base encoding.")
        encoding = tiktoken.get_encoding("cl100k_base")
    if model in {
        "gpt-3.5-turbo-0613",
        "gpt-3.5-turbo-16k-0613",
        "gpt-4-0314",
        "gpt-4-32k-0314",
        "gpt-4-0613",
        "gpt-4-32k-0613",
    }:
        tokens_per_message = 3
        tokens_per_name = 1
    elif model == "gpt-3.5-turbo-0301":
        tokens_per_message = 4  # every message follows <|start|>{role/name}\n{content}<|end|>\n
        tokens_per_name = -1  # if there's a name, the role is omitted
    elif "gpt-3.5-turbo" in model:
        if not silent:
            print(
                "Warning: gpt-3.5-turbo may update over time. "
                "Returning num tokens assuming gpt-3.5-turbo-0613."
            )
        return num_tokens_from_messages(messages, model="gpt-3.5-turbo-0613")
    elif "gpt-4" in model:
        if not silent:
            print("Warning: gpt-4 may update over time. Returning num tokens assuming gpt-4-0613.")
        return num_tokens_from_messages(messages, model="gpt-4-0613")
    else:
        raise NotImplementedError(
            f"""num_tokens_from_messages() is not implemented for model {model}.
            See https://github.com/openai/openai-python/blob/main/chatml.md for information on
            how messages are converted to tokens."""
        )
    num_tokens = 0
    for message in messages:
        num_tokens += tokens_per_message
        for key, value in message.items():
            assert isinstance(value, str)
            num_tokens += len(encoding.encode(value))
            if key == "name":
                num_tokens += tokens_per_name
    num_tokens += 3  # every reply is primed with <|start|>assistant<|message|>
    return num_tokens


ReportGeneratedCallback = Callable[[str, int], None]


class ReportGenerator:
    def __init__(
        self,
        api_key: str,
        model: str = "gpt-3.5-turbo",
        max_tokens: int = 4096,
        language: Literal["en", "de"] = "en",
        callback: ReportGeneratedCallback | None = None,
    ) -> None:
        self._client = OpenAI(api_key=api_key)
        self._model = model
        self._max_tokens = max_tokens
        self._language = language
        self._callback = callback

        self.messages: list[ChatCompletionMessageParam] = [
            {"role": "system", "content": INITIAL_SYSTEM_PROMPT[self._language]}
        ]

    def reset_context(self, full_reset=False):
        self.messages = [self.messages[0]]

    def generate_report(self) -> str:
        if len(self.messages) == 1:
            self.messages.append({"role": "user", "content": INITIAL_INSTRUCTION[self._language]})
        else:
            self.messages.append({"role": "user", "content": FOLLOWUP_INSTRUCTION[self._language]})

        token_count = num_tokens_from_messages(self.messages, self._model, silent=True)
        if token_count > self._max_tokens:
            # Retain system prompt, initial instruction, last answer and last instruction
            assert len(self.messages) >= 4
            self.messages = [
                self.messages[0],
                self.messages[1],
                self.messages[-2],
                self.messages[-1],
            ]

        response: Any = None
        retries = 0
        while not response:
            try:
                response = self._client.chat.completions.create(
                    messages=self.messages,
                    model=self._model,
                    # model=self.model, messages=self.messages, api_key=self.api_key
                )
            # For available errors see https://github.com/openai/openai-python#handling-errors
            except openai.APIStatusError as err:
                retries += 1
                if retries == 3:
                    print(f"Error! Service unavailable even after 3 retries: {err}")
                    raise err

                # maybe use rate limiter like https://github.com/tomasbasham/ratelimit
                sleep(randint(3, 10))

        answer = response.choices[0].message.content

        if self._callback:
            token_count = num_tokens_from_messages(self.messages, self._model, silent=True)
            self._callback(answer, token_count)

        self.messages.append({"role": "assistant", "content": answer})
        return answer
