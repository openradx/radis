from typing import Literal, get_args, get_origin

import pytest
from django.core.exceptions import ValidationError

from radis.extractions.factories import ExtractionJobFactory, OutputFieldFactory
from radis.extractions.models import OutputType
from radis.extractions.utils.processor_utils import generate_output_fields_schema


def _without_null(annotation: object) -> object:
    """Return the value type of a nullable field annotation (``T | None`` -> ``T``)."""
    (value_type,) = [arg for arg in get_args(annotation) if arg is not type(None)]
    return value_type


@pytest.mark.django_db
def test_generate_output_fields_schema_uses_literal_for_selection_fields():
    job = ExtractionJobFactory.create()
    field = OutputFieldFactory(
        job=job,
        name="grade",
        output_type=OutputType.SELECTION,
    )
    field.selection_options = ["Grade 1", "Grade 2"]
    field.save()

    schema = generate_output_fields_schema(job.output_fields.all())

    annotation = _without_null(schema.model_fields["grade"].annotation)
    assert get_origin(annotation) is Literal
    assert set(get_args(annotation)) == {"Grade 1", "Grade 2"}


@pytest.mark.django_db
def test_generate_output_fields_schema_wraps_literal_in_list_for_array_selections():
    job = ExtractionJobFactory.create()
    field = OutputFieldFactory(
        job=job,
        name="grade_multi",
        output_type=OutputType.SELECTION,
    )
    field.selection_options = ["High", "Low"]
    field.is_array = True
    field.save()

    schema = generate_output_fields_schema(job.output_fields.all())

    annotation = _without_null(schema.model_fields["grade_multi"].annotation)
    assert get_origin(annotation) is list
    (inner_annotation,) = get_args(annotation)
    assert get_origin(inner_annotation) is Literal
    assert set(get_args(inner_annotation)) == {"High", "Low"}


@pytest.mark.django_db
def test_generate_output_fields_schema_accepts_null_for_every_output_type():
    """Every value may be null so the model can say "not in the report" instead of
    fabricating a value; the keys themselves stay required."""
    job = ExtractionJobFactory.create()
    OutputFieldFactory(job=job, name="finding", output_type=OutputType.TEXT)
    OutputFieldFactory(job=job, name="laenge_mm", output_type=OutputType.NUMERIC)
    OutputFieldFactory(job=job, name="malignant", output_type=OutputType.BOOLEAN)
    selection = OutputFieldFactory(job=job, name="grade", output_type=OutputType.SELECTION)
    selection.selection_options = ["Grade 1", "Grade 2"]
    selection.save()
    OutputFieldFactory(job=job, name="vertebrae", output_type=OutputType.TEXT, is_array=True)

    schema = generate_output_fields_schema(job.output_fields.all())

    instance = schema(finding=None, laenge_mm=None, malignant=None, grade=None, vertebrae=None)
    assert instance.model_dump() == {
        "finding": None,
        "laenge_mm": None,
        "malignant": None,
        "grade": None,
        "vertebrae": None,
    }

    # Non-null values still validate...
    instance = schema(
        finding="pneumothorax", laenge_mm=12.5, malignant=True, grade="Grade 1", vertebrae=["L1"]
    )
    assert instance.grade == "Grade 1"  # type: ignore[attr-defined]

    # ...and the keys themselves stay required.
    with pytest.raises(ValueError):
        schema(finding="pneumothorax")


@pytest.mark.django_db
def test_generate_output_fields_schema_json_schema_offers_null_option():
    """The JSON schema sent to the model must list null as an alternative; a bare
    required ``$ref`` forces the model to produce a value for every field."""
    job = ExtractionJobFactory.create()
    OutputFieldFactory(job=job, name="laenge_mm", output_type=OutputType.NUMERIC)

    json_schema = generate_output_fields_schema(job.output_fields.all()).model_json_schema()

    assert json_schema["required"] == ["laenge_mm"]
    assert {"type": "null"} in json_schema["properties"]["laenge_mm"]["anyOf"]


@pytest.mark.django_db
@pytest.mark.parametrize("name", ["_dropped", "model_config"])
def test_output_field_names_that_break_the_schema_fail_validation(name: str):
    """Names Pydantic drops (``_x``) or rejects (``model_*``) never reach ``create_model``."""
    field = OutputFieldFactory.build(job=ExtractionJobFactory.create(), name=name)

    with pytest.raises(ValidationError) as excinfo:
        field.full_clean()

    assert "name" in excinfo.value.message_dict
