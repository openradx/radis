from enum import StrEnum
from typing import Sequence

from pydantic import BaseModel, Field, create_model


class BucketValue(StrEnum):
    PRESENT = "PRESENT"
    LIKELY = "LIKELY"
    POSSIBLE = "POSSIBLE"
    ABSENT = "ABSENT"
    UNMENTIONED = "UNMENTIONED"


class GateValue(StrEnum):
    YES = "YES"
    NO = "NO"
    MAYBE = "MAYBE"


def build_label_classification_schema(labels: Sequence) -> type[BaseModel]:
    """Build a Pydantic model with one required `label_<id>` field per label.

    The enum type is static (the five buckets); only the set of fields is dynamic.
    Each label's name + description rides in the field description= so the LLM reads it
    via the JSON schema. The answer is keyed purely by id — immune to renames.
    """
    fields = {
        f"label_{lbl.id}": (BucketValue, Field(description=f"{lbl.name}: {lbl.description}"))
        for lbl in labels
    }
    return create_model("LabelClassification", **fields)


def build_gate_schema(groups: Sequence) -> type[BaseModel]:
    """Build a Pydantic model with one required `group_<id>` field per group."""
    fields = {
        f"group_{g.id}": (GateValue, Field(description=g.gate_question)) for g in groups
    }
    return create_model("GateScreening", **fields)


def parse_label_results(parsed: BaseModel) -> dict[int, str]:
    return {int(k.removeprefix("label_")): str(v) for k, v in parsed.model_dump().items()}


def parse_gate_results(parsed: BaseModel) -> dict[int, str]:
    return {int(k.removeprefix("group_")): str(v) for k, v in parsed.model_dump().items()}
