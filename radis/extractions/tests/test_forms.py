import json

import pytest
from adit_radis_shared.accounts.factories import GroupFactory, UserFactory
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError

from radis.extractions.factories import ExtractionJobFactory
from radis.extractions.forms import OutputFieldForm, SearchForm
from radis.extractions.models import OutputField, OutputType
from radis.reports.factories import LanguageFactory


@pytest.mark.django_db
def test_output_field_form_accepts_selection_options():
    job = ExtractionJobFactory.create()
    form = OutputFieldForm(
        data={
            "name": "tumor_grade",
            "description": "Classified tumor grade.",
            "output_type": OutputType.SELECTION,
            "selection_options": json.dumps(["Grade 1", "Grade 2"]),
            "is_array": "false",
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
            "is_array": "false",
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
            "is_array": "false",
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


@pytest.mark.django_db
def test_output_field_form_handles_array_toggle():
    job = ExtractionJobFactory.create()
    form = OutputFieldForm(
        data={
            "name": "measurements",
            "description": "Multiple numeric values.",
            "output_type": OutputType.NUMERIC,
            "selection_options": json.dumps([]),
            "is_array": "true",
        },
        instance=OutputField(job=job),
    )

    assert form.is_valid()
    instance = form.save(commit=False)
    assert instance.is_array is True


@pytest.mark.django_db
def test_output_field_form_rejects_duplicate_selection_options():
    job = ExtractionJobFactory.create()
    form = OutputFieldForm(
        data={
            "name": "tumor_grade",
            "description": "Classified tumor grade.",
            "output_type": OutputType.SELECTION,
            "selection_options": json.dumps(["Grade 1", "Grade 1"]),
            "is_array": "false",
        },
        instance=OutputField(job=job),
    )

    assert not form.is_valid()
    assert "selection_options" in form.errors


@pytest.mark.django_db
def test_output_field_clean_rejects_duplicate_selection_options():
    job = ExtractionJobFactory.create()
    field = OutputField(
        job=job,
        name="tumor_grade",
        description="Classified tumor grade.",
        output_type=OutputType.SELECTION,
        selection_options=["Grade 1", "Grade 1"],
    )

    with pytest.raises(ValidationError):
        field.full_clean()


@pytest.mark.django_db
def test_output_field_form_rejects_whitespace_only_selection_option():
    job = ExtractionJobFactory.create()
    form = OutputFieldForm(
        data={
            "name": "tumor_grade",
            "description": "Classified tumor grade.",
            "output_type": OutputType.SELECTION,
            "selection_options": json.dumps(["Grade 1", "   "]),
            "is_array": "false",
        },
        instance=OutputField(job=job),
    )

    assert not form.is_valid()
    assert "selection_options" in form.errors


@pytest.mark.django_db
def test_output_field_form_accepts_unicode_and_long_selection_options():
    job = ExtractionJobFactory.create()
    unicode_option = "Grädé αβγ測試"
    long_option = "Grade " + ("X" * 150)
    form = OutputFieldForm(
        data={
            "name": "tumor_grade",
            "description": "Classified tumor grade.",
            "output_type": OutputType.SELECTION,
            "selection_options": json.dumps([unicode_option, long_option]),
            "is_array": "false",
        },
        instance=OutputField(job=job),
    )

    assert form.is_valid()
    instance = form.save(commit=False)
    assert instance.selection_options == [unicode_option.strip(), long_option.strip()]


def _layout_html_blobs(layout) -> str:
    collected: list[str] = []

    def walk(item):
        html = getattr(item, "html", None)
        if isinstance(html, str):
            collected.append(html)
        for child in getattr(item, "fields", []):
            walk(child)

    walk(layout)
    return " ".join(collected)


@pytest.mark.django_db
def test_search_form_texts_without_auto_query_generation(settings):
    """With auto generation disabled, no auto-generation wording is shown."""
    from django.contrib.auth import get_user_model

    from radis.extractions.forms import SearchForm

    settings.ENABLE_AUTO_QUERY_GENERATION = False
    form = SearchForm(user=get_user_model()())

    assert "auto-generated" not in form.fields["query"].help_text.lower()
    assert "auto-generated" not in form.fields["query"].widget.attrs["placeholder"].lower()
    assert "_query_generation_section" not in _layout_html_blobs(form.helper.layout)


@pytest.mark.django_db
def test_search_form_includes_generation_section_when_enabled(settings):
    from django.contrib.auth import get_user_model

    from radis.extractions.forms import SearchForm

    settings.ENABLE_AUTO_QUERY_GENERATION = True
    form = SearchForm(user=get_user_model()())

    assert "_query_generation_section" in _layout_html_blobs(form.helper.layout)


@pytest.mark.django_db
def test_language_field_offers_all_and_is_the_default():
    """The language filter offers 'All' as a choice and is pre-selected on an
    unbound form, mirroring the search form's behavior."""
    LanguageFactory.create(code="en")
    LanguageFactory.create(code="de")

    form = SearchForm(user=get_user_model()())
    field = form.fields["language"]

    choices = list(field.choices)  # type: ignore[attr-defined]
    assert choices[0] == ("", "All")
    # No data and no initial -> the bound field's rendered value is falsy,
    # which is what makes the browser default to the first ("All") option.
    assert not form["language"].value()


@pytest.mark.django_db
def test_job_saved_with_no_language_persists_none():
    """Submitting the search step with 'All' selected must save language=None
    on the ExtractionJob rather than raising or falling back to some language."""
    LanguageFactory.create(code="en")
    user = UserFactory.create(is_active=True)
    group = GroupFactory.create()
    user.groups.add(group)
    user.active_group = group
    user.save()

    data = {
        "title": "My Extraction",
        "query": "pneumonia",
        "language": "",
        "modalities": [],
        "study_date_from": "",
        "study_date_till": "",
        "study_description": "",
        "patient_sex": "",
        "age_from": "",
        "age_till": "",
    }
    form = SearchForm(data=data, user=user)

    assert form.is_valid(), form.errors
    assert form.cleaned_data["language"] is None

    job = form.save(commit=False)
    job.owner = user
    job.group = group
    job.save()
    job.refresh_from_db()

    assert job.language is None
