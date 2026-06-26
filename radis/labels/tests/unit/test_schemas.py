from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from radis.labels.models import GateAnswer, LabelResult
from radis.labels.utils.schemas import (
    BucketValue,
    GateValue,
    build_gate_schema,
    build_label_classification_schema,
)


def _label(id, name="pneumonia", description="infection of the lung"):
    return SimpleNamespace(id=id, name=name, description=description)


def _group(id, name="pulmonary", gate_question="Is this a chest study?"):
    return SimpleNamespace(id=id, name=name, gate_question=gate_question)


def test_label_schema_fields_are_name_keyed_and_carry_description():
    Schema = build_label_classification_schema([_label(42, "pneumonia", "lung infection")])
    assert "pneumonia" in Schema.model_fields
    assert Schema.model_fields["pneumonia"].description == "lung infection"


def test_label_schema_accepts_all_five_buckets_and_rejects_unknown():
    Schema = build_label_classification_schema([_label(1)])
    for bucket in ("PRESENT", "LIKELY", "POSSIBLE", "ABSENT", "UNMENTIONED"):
        assert Schema.model_validate({"pneumonia": bucket}).model_dump()["pneumonia"] == bucket
    with pytest.raises(ValidationError):
        Schema.model_validate({"pneumonia": "BOGUS"})


def test_label_schema_field_is_required():
    Schema = build_label_classification_schema([_label(1)])
    with pytest.raises(ValidationError):
        Schema.model_validate({})


def test_gate_schema_fields_are_name_keyed_and_carry_question():
    Schema = build_gate_schema([_group(7, "pulmonary", "Does this report mention the lungs?")])
    assert "pulmonary" in Schema.model_fields
    assert Schema.model_fields["pulmonary"].description == "Does this report mention the lungs?"


def test_gate_schema_validates_yes_no_and_rejects_unknown():
    Schema = build_gate_schema([_group(1)])
    for value in ("YES", "NO"):
        assert Schema.model_validate({"pulmonary": value}).model_dump()["pulmonary"] == value
    with pytest.raises(ValidationError):
        Schema.model_validate({"pulmonary": "MAYBE"})
    with pytest.raises(ValidationError):
        Schema.model_validate({"pulmonary": "PROBABLY"})


def test_bucket_and_gate_enums_match_model_choices():
    """Drift guard: static enums must stay in sync with the model TextChoices."""
    assert {b.value for b in BucketValue} == {c.value for c in LabelResult.Value}
    assert {g.value for g in GateValue} == {c.value for c in GateAnswer.Value}


# Field names are dict keys passed via **kwargs to pydantic's create_model, so they are stored
# as plain strings — they need NOT be valid Python identifiers. This lets label/group names be
# free text (spaces, hyphens, digits) without sanitization, and round-trips through model_dump
# keyed by that exact name (which is how labeling.py reads results back).
NON_IDENTIFIER_NAMES = ["aortic aneurysm", "covid-19", "2-vessel disease", "post-op change"]


@pytest.mark.parametrize("name", NON_IDENTIFIER_NAMES)
def test_label_schema_accepts_non_identifier_names(name):
    Schema = build_label_classification_schema([_label(1, name, "some description")])
    assert name in Schema.model_fields
    assert Schema.model_fields[name].description == "some description"
    dumped = Schema.model_validate({name: "PRESENT"}).model_dump()
    assert dumped[name] == BucketValue.PRESENT


@pytest.mark.parametrize("name", NON_IDENTIFIER_NAMES)
def test_gate_schema_accepts_non_identifier_names(name):
    Schema = build_gate_schema([_group(1, name, "a screening question?")])
    assert name in Schema.model_fields
    assert Schema.model_fields[name].description == "a screening question?"
    dumped = Schema.model_validate({name: "YES"}).model_dump()
    assert dumped[name] == GateValue.YES


def test_label_schema_keeps_multiple_non_identifier_names_distinct():
    """Several free-text labels in one schema each get their own field and round-trip cleanly."""
    labels = [_label(i, name, f"desc {i}") for i, name in enumerate(NON_IDENTIFIER_NAMES)]
    Schema = build_label_classification_schema(labels)
    assert set(Schema.model_fields) == set(NON_IDENTIFIER_NAMES)
    payload = {name: "ABSENT" for name in NON_IDENTIFIER_NAMES}
    dumped = Schema.model_validate(payload).model_dump()
    assert all(dumped[name] == BucketValue.ABSENT for name in NON_IDENTIFIER_NAMES)
