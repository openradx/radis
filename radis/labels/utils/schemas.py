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


def gate_field_name(group) -> str:
    """Structured-output field name for a group's gate answer: the gate question itself.

    Deliberately not the group's display name. In structured output the model answers
    the field *name* far more strongly than the field description, so a display name
    like "Acute abdomen" makes it judge the diagnosis ("is this an acute abdomen?")
    instead of the scope ("is this abdominal imaging?"). It then answers NO for most
    routine abdominal studies, and their findings are never computed — silently, with
    no row to audit.

    The question is used verbatim, with only surrounding whitespace stripped, because
    rewriting it could change what the model is asked. Groups that share a question
    share one field and therefore one answer.
    """
    return group.gate_question.strip()


def build_gate_schema(groups: Sequence) -> type[BaseModel]:
    fields: dict[str, Any] = {
        gate_field_name(g): (GateValue, Field(description=g.gate_question)) for g in groups
    }
    return create_model("GateScreening", **fields)
