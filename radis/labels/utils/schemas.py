from collections.abc import Sequence
from enum import StrEnum
from typing import Any

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


def build_label_classification_schema(labels: Sequence) -> type[BaseModel]:
    fields: dict[str, Any] = {
        lbl.name: (BucketValue, Field(description=lbl.description)) for lbl in labels
    }
    return create_model("LabelClassification", **fields)


def build_gate_schema(groups: Sequence) -> type[BaseModel]:
    fields: dict[str, Any] = {
        g.name: (GateValue, Field(description=g.gate_question)) for g in groups
    }
    return create_model("GateScreening", **fields)
