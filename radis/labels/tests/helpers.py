"""Test double for ChatClient used by label_report.

extract_data inspects the dynamically-built schema's field names (`group_<id>` or
`label_<id>`) and returns a valid instance populated from the configured answer maps.
Recorded calls let tests assert exact gate vs. label LLM call counts.
"""
from pydantic import BaseModel


class FakeChatClient:
    def __init__(self, gate_values: dict[int, str] | None = None,
                 label_values: dict[int, str] | None = None) -> None:
        self.gate_values = gate_values or {}
        self.label_values = label_values or {}
        self.gate_calls: list[list[int]] = []
        self.label_calls: list[list[int]] = []

    def extract_data(self, prompt: str, schema: type[BaseModel]) -> BaseModel:
        data: dict[str, str] = {}
        field_names = list(schema.model_fields.keys())
        if field_names and field_names[0].startswith("group_"):
            ids = [int(n.removeprefix("group_")) for n in field_names]
            self.gate_calls.append(ids)
            for n, gid in zip(field_names, ids):
                data[n] = self.gate_values[gid]
        else:
            ids = [int(n.removeprefix("label_")) for n in field_names]
            self.label_calls.append(ids)
            for n, lid in zip(field_names, ids):
                data[n] = self.label_values[lid]
        return schema(**data)
