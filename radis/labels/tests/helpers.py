"""Test doubles for the LLM boundary used by ``label_report``.

- ``FakeChatClient`` replaces ``ChatClient`` outright (unit tests).
- ``create_labeling_openai_mock`` patches ``openai.OpenAI`` so the real ``ChatClient`` runs with
  only the network mocked (integration test).
"""

from unittest.mock import MagicMock

from pydantic import BaseModel


class FakeChatClient:
    def __init__(
        self,
        gate_values: dict[str, str] | None = None,
        label_values: dict[str, str] | None = None,
    ) -> None:
        self.gate_values = gate_values or {}
        self.label_values = label_values or {}
        self.gate_calls: list[list[str]] = []
        self.label_calls: list[list[str]] = []

    def extract_data(self, prompt: str, schema: type[BaseModel]) -> BaseModel:
        field_names = list(schema.model_fields.keys())
        if schema.__name__ == "GateScreening":
            self.gate_calls.append(field_names)
            data = {name: self.gate_values[name] for name in field_names}
        else:
            self.label_calls.append(field_names)
            data = {name: self.label_values[name] for name in field_names}
        return schema.model_validate(data)


def create_labeling_openai_mock(
    gate_values: dict[str, str] | None = None,
    label_values: dict[str, str] | None = None,
) -> MagicMock:
    """An ``openai.OpenAI`` double for the two-phase flow: ``parse`` returns an instance of
    whichever schema each call requests — gate ("GateScreening") from ``gate_values``, label
    ("LabelClassification") from ``label_values``, keyed by group/label name. Patch via
    ``patch("openai.OpenAI", return_value=...)`` so the real ``ChatClient`` runs.
    """
    gate_values = gate_values or {}
    label_values = label_values or {}

    def _parse(**kwargs) -> MagicMock:
        schema: type[BaseModel] = kwargs["response_format"]
        names = list(schema.model_fields.keys())
        source = gate_values if schema.__name__ == "GateScreening" else label_values
        parsed = schema.model_validate({name: source[name] for name in names})
        return MagicMock(choices=[MagicMock(message=MagicMock(parsed=parsed))])

    client = MagicMock()
    client.beta.chat.completions.parse.side_effect = _parse
    return client
