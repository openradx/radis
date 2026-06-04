from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from radis.labels.models import GateAnswer, LabelResult
from radis.labels.utils.schemas import (
    BucketValue,
    GateValue,
    build_gate_schema,
    build_label_classification_schema,
    parse_gate_results,
    parse_label_results,
)


def _label(id, name="pneumonia", description="infection of the lung"):
    return SimpleNamespace(id=id, name=name, description=description)


def _group(id, gate_question="Is this a chest study?"):
    return SimpleNamespace(id=id, gate_question=gate_question)


def test_label_schema_fields_are_id_keyed_and_carry_name_and_description():
    Schema = build_label_classification_schema([_label(42, "pneumonia", "lung infection")])
    assert "label_42" in Schema.model_fields
    field = Schema.model_fields["label_42"]
    assert "pneumonia" in field.description
    assert "lung infection" in field.description


def test_label_schema_accepts_all_five_buckets_and_rejects_unknown():
    Schema = build_label_classification_schema([_label(1)])
    for bucket in ("PRESENT", "LIKELY", "POSSIBLE", "ABSENT", "UNMENTIONED"):
        assert Schema(label_1=bucket).label_1 == bucket
    with pytest.raises(ValidationError):
        Schema(label_1="BOGUS")


def test_label_schema_field_is_required():
    Schema = build_label_classification_schema([_label(1)])
    with pytest.raises(ValidationError):
        Schema()  # missing label_1


def test_gate_schema_fields_are_id_keyed_and_carry_question():
    Schema = build_gate_schema([_group(7, "Does this report mention the lungs?")])
    assert "group_7" in Schema.model_fields
    assert "lungs" in Schema.model_fields["group_7"].description


def test_gate_schema_validates_yes_no_maybe_and_rejects_unknown():
    Schema = build_gate_schema([_group(1)])
    for value in ("YES", "NO", "MAYBE"):
        assert Schema(group_1=value).group_1 == value
    with pytest.raises(ValidationError):
        Schema(group_1="PROBABLY")


def test_parse_label_results_round_trips_ids():
    Schema = build_label_classification_schema([_label(1), _label(2, "edema", "fluid")])
    parsed = Schema(label_1="PRESENT", label_2="ABSENT")
    assert parse_label_results(parsed) == {1: "PRESENT", 2: "ABSENT"}


def test_parse_gate_results_round_trips_ids():
    Schema = build_gate_schema([_group(3), _group(4)])
    parsed = Schema(group_3="YES", group_4="NO")
    assert parse_gate_results(parsed) == {3: "YES", 4: "NO"}


def test_bucket_and_gate_enums_match_model_choices():
    """Drift guard: static enums must stay in sync with the model TextChoices."""
    assert {b.value for b in BucketValue} == {c.value for c in LabelResult.Value}
    assert {g.value for g in GateValue} == {c.value for c in GateAnswer.Value}
