import re
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
    """Structured-output field name for a group's gate answer.

    Derived from the gate *question*, deliberately NOT from the group's display
    name. In structured output the model answers the field *name* far more
    strongly than the field description, so the name has to carry the actual
    scoping question. A display name like "Acute abdomen" makes the model judge
    the diagnosis ("is this an acute abdomen?") instead of the scope ("is this
    abdominal imaging?"), so it answers NO for most routine abdominal studies and
    their findings are never computed — silently, with no row to audit. Measured
    on real reports, keying by the question instead of the name cut the false-NO
    rate on abdominal studies from ~88% to ~18%. The trailing id keeps the field
    names unique even if two groups happen to share a question.
    """
    slug = re.sub(r"[^a-z0-9]+", "_", (group.gate_question or "").lower()).strip("_")
    return f"{slug or 'gate'}_{group.id}"


def build_gate_schema(groups: Sequence) -> type[BaseModel]:
    fields: dict[str, Any] = {
        gate_field_name(g): (GateValue, Field(description=g.gate_question)) for g in groups
    }
    return create_model("GateScreening", **fields)
