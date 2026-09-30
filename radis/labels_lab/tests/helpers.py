from pydantic import BaseModel

from radis.labels.tests.helpers import FakeChatClient


class FakeLLM(FakeChatClient):
    """The labels app's LLM double, which also takes the wait budget the lab passes along."""

    def __init__(
        self,
        gate_values: dict[str, str] | None = None,
        label_values: dict[str, str] | None = None,
    ) -> None:
        super().__init__(gate_values, label_values)
        self.max_waits: list[float | None] = []

    def extract_data(
        self, prompt: str, schema: type[BaseModel], max_wait: float | None = None
    ) -> BaseModel:
        self.max_waits.append(max_wait)
        return super().extract_data(prompt, schema)
