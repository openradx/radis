from typing import Literal, get_args, get_origin

import pytest

from radis.extractions.factories import ExtractionJobFactory, OutputFieldFactory
from radis.extractions.models import OutputType
from radis.extractions.utils.processor_utils import generate_output_fields_schema


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

    grade_field = schema.model_fields["grade"]
    annotation = grade_field.annotation
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

    grade_field = schema.model_fields["grade_multi"]
    annotation = grade_field.annotation
    assert get_origin(annotation) is list
    (inner_annotation,) = get_args(annotation)
    assert get_origin(inner_annotation) is Literal
    assert set(get_args(inner_annotation)) == {"High", "Low"}
