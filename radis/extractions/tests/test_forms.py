import json

import pytest
from django.core.exceptions import ValidationError

from radis.extractions.factories import ExtractionJobFactory
from radis.extractions.forms import OutputFieldForm
from radis.extractions.models import OutputField, OutputType


@pytest.mark.django_db
def test_output_field_form_accepts_selection_options():
    job = ExtractionJobFactory.create()
    form = OutputFieldForm(
        data={
            "name": "tumor_grade",
            "description": "Classified tumor grade.",
            "output_type": OutputType.SELECTION,
            "selection_options": json.dumps(["Grade 1", "Grade 2"]),
        },
        instance=OutputField(job=job),
    )

    assert form.is_valid()
    instance = form.save(commit=False)

    assert instance.selection_options == ["Grade 1", "Grade 2"]


@pytest.mark.django_db
def test_output_field_form_requires_options_for_selection():
    job = ExtractionJobFactory.create()
    form = OutputFieldForm(
        data={
            "name": "tumor_grade",
            "description": "Classified tumor grade.",
            "output_type": OutputType.SELECTION,
            "selection_options": json.dumps([]),
        },
        instance=OutputField(job=job),
    )

    assert not form.is_valid()
    assert "selection_options" in form.errors


@pytest.mark.django_db
def test_output_field_form_rejects_options_for_non_selection():
    job = ExtractionJobFactory.create()
    form = OutputFieldForm(
        data={
            "name": "tumor_grade",
            "description": "Classified tumor grade.",
            "output_type": OutputType.TEXT,
            "selection_options": json.dumps(["Grade 1"]),
        },
        instance=OutputField(job=job),
    )

    assert not form.is_valid()
    assert "selection_options" in form.errors


@pytest.mark.django_db
def test_output_field_clean_trims_selection_options():
    job = ExtractionJobFactory.create()
    field = OutputField(
        job=job,
        name="tumor_grade",
        description="Classified tumor grade.",
        output_type=OutputType.SELECTION,
        selection_options=["  Grade 1 ", "Grade 2  "],
    )

    field.full_clean()

    assert field.selection_options == ["Grade 1", "Grade 2"]


@pytest.mark.django_db
def test_output_field_clean_rejects_selection_options_for_other_types():
    job = ExtractionJobFactory.create()
    field = OutputField(
        job=job,
        name="tumor_grade",
        description="Classified tumor grade.",
        output_type=OutputType.TEXT,
        selection_options=["Grade 1"],
    )

    with pytest.raises(ValidationError):
        field.full_clean()
