"""Test double for ChatClient used by label_report.

extract_data inspects the dynamically-built schema's __name__ to decide whether this is
a gate call ("GateScreening") or a label call ("LabelClassification"), then returns a
valid instance populated from the configured answer maps (keyed by group/label name).
Recorded calls let tests assert exact gate vs. label LLM call counts.
"""

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
