import datetime
from types import SimpleNamespace

import pytest
from django import forms
from django.contrib.auth.models import Group

from radis.core.date_formats import DATE_INPUT_FORMATS
from radis.extractions.forms import SearchForm
from radis.extractions.site import (
    ExtractionRetrievalProvider,
    extraction_retrieval_providers,
    register_extraction_retrieval_provider,
)
from radis.reports.models import Language, Modality


@pytest.fixture(autouse=True)
def mock_extraction_provider():
    extraction_retrieval_providers.clear()

    provider = ExtractionRetrievalProvider(
        name="test-provider",
        count=lambda search: 0,
        retrieve=lambda search: [],
        max_results=None,
    )
    register_extraction_retrieval_provider(provider)
    yield provider
    extraction_retrieval_providers.clear()


@pytest.mark.django_db
def test_extraction_form_uses_shared_input_formats():
    date_from_field = SearchForm.base_fields["study_date_from"]
    date_till_field = SearchForm.base_fields["study_date_till"]

    assert isinstance(date_from_field, forms.DateField)
    assert isinstance(date_till_field, forms.DateField)
    assert date_from_field.input_formats == DATE_INPUT_FORMATS
    assert date_till_field.input_formats == DATE_INPUT_FORMATS


@pytest.mark.django_db
def test_extraction_form_accepts_day_first_date(mock_extraction_provider):
    group = Group.objects.create(name="Active Group")
    language = Language.objects.create(code="en")
    modality = Modality.objects.create(code="CT")
    user = SimpleNamespace(active_group=group)

    form = SearchForm(
        data={
            "title": "Brain study",
            "provider": mock_extraction_provider.name,
            "query": "brain",
            "language": language.pk,
            "modalities": [modality.pk],
            "study_date_from": "14/02/2025",
            "patient_sex": "",
        },
        user=user,
    )

    assert form.is_valid()
    assert form.cleaned_data["study_date_from"] == datetime.date(2025, 2, 14)


@pytest.mark.django_db
def test_extraction_form_rejects_inverted_date_range(mock_extraction_provider):
    group = Group.objects.create(name="Active Group")
    language = Language.objects.create(code="en")
    user = SimpleNamespace(active_group=group)

    form = SearchForm(
        data={
            "title": "Brain study",
            "provider": mock_extraction_provider.name,
            "query": "brain",
            "language": language.pk,
            "study_date_from": "15/02/2025",
            "study_date_till": "14/02/2025",
        },
        user=user,
    )

    assert not form.is_valid()
    assert (
        "Study date from must be before study date till" in form.errors["__all__"]
    )
