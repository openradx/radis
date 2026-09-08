from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from radis.labels.models import GateAnswer, LabelResult
from radis.labels.utils.schemas import (
    BucketValue,
    GateValue,
    build_gate_schema,
    build_label_classification_schema,
    gate_field_name,
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


def test_gate_field_name_is_derived_from_the_question_not_the_group_name():
    # The model answers the structured-output field *name* much more strongly than the
    # description, so the name must be the scoping question. A group name that is a
    # diagnosis ("Acute abdomen") must never become the field the model answers.
    g = _group(2, "Acute abdomen", "Does this report describe imaging of the abdomen or pelvis?")
    fname = gate_field_name(g)
    assert "abdomen" in fname and "imaging" in fname  # the question drives the name
    assert "acute" not in fname  # the diagnosis display name does not leak in


def test_gate_schema_is_keyed_by_question_not_by_group_name():
    g = _group(2, "Acute abdomen", "Does this report describe imaging of the abdomen or pelvis?")
    Schema = build_gate_schema([g])
    assert "Acute abdomen" not in Schema.model_fields  # regression guard for the field-name bug
    assert gate_field_name(g) in Schema.model_fields
    assert Schema.model_fields[gate_field_name(g)].description == g.gate_question


def test_gate_schema_validates_yes_no_and_rejects_unknown():
    g = _group(1)
    Schema = build_gate_schema([g])
    key = gate_field_name(g)
    for value in ("YES", "NO"):
        assert Schema.model_validate({key: value}).model_dump()[key] == value
    with pytest.raises(ValidationError):
        Schema.model_validate({key: "MAYBE"})
    with pytest.raises(ValidationError):
        Schema.model_validate({key: "PROBABLY"})


def test_gate_field_names_stay_unique_when_two_groups_share_a_question():
    q = "Does this report describe imaging of the abdomen or pelvis?"
    groups = [_group(2, "Acute abdomen", q), _group(5, "GI tract", q)]
    Schema = build_gate_schema(groups)
    assert len(Schema.model_fields) == 2  # neither group's field was swallowed by a collision
    assert set(Schema.model_fields) == {gate_field_name(g) for g in groups}


def test_gate_results_round_trip_by_gate_field_name():
    # labeling.py reads each group's answer back via gate_field_name(group); guard that contract.
    groups = [
        _group(2, "Acute abdomen", "Does this report describe imaging of the abdomen or pelvis?"),
        _group(3, "Neuroimaging (head)", "Does this report describe imaging of the head or brain?"),
    ]
    Schema = build_gate_schema(groups)
    payload = {gate_field_name(g): "YES" for g in groups}
    dumped = Schema.model_validate(payload).model_dump()
    assert all(dumped[gate_field_name(g)] == GateValue.YES for g in groups)


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
def test_gate_field_name_never_leaks_the_group_display_name(name):
    # However the group is named — including diagnosis-like names — the gate field is
    # keyed by the question, and the display name never becomes the field the model answers.
    g = _group(1, name, "a screening question?")
    Schema = build_gate_schema([g])
    assert name not in Schema.model_fields
    assert list(Schema.model_fields) == [gate_field_name(g)]
    assert Schema.model_fields[gate_field_name(g)].description == "a screening question?"


def test_label_schema_keeps_multiple_non_identifier_names_distinct():
    """Several free-text labels in one schema each get their own field and round-trip cleanly."""
    labels = [_label(i, name, f"desc {i}") for i, name in enumerate(NON_IDENTIFIER_NAMES)]
    Schema = build_label_classification_schema(labels)
    assert set(Schema.model_fields) == set(NON_IDENTIFIER_NAMES)
    payload = {name: "ABSENT" for name in NON_IDENTIFIER_NAMES}
    dumped = Schema.model_validate(payload).model_dump()
    assert all(dumped[name] == BucketValue.ABSENT for name in NON_IDENTIFIER_NAMES)
