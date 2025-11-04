import datetime

import pytest

from radis.reports.models import Language, Modality
from radis.search.forms import SearchForm
from radis.search.site import (
    SearchProvider,
    SearchResult,
    register_search_provider,
    search_providers,
)


@pytest.fixture(autouse=True)
def mock_search_provider():
    search_providers.clear()

    def _search(_search):
        return SearchResult(total_count=0, total_relation="exact", documents=[])

    provider = SearchProvider(name="test-provider", search=_search, max_results=100)
    register_search_provider(provider)
    yield provider
    search_providers.clear()


@pytest.mark.django_db
def test_search_form_accepts_day_first_date(mock_search_provider):
    form = SearchForm(
        data={
            "provider": mock_search_provider.name,
            "study_date_from": "14/02/2025",
        }
    )
    assert form.is_valid()
    assert form.cleaned_data["study_date_from"] == datetime.date(2025, 2, 14)


@pytest.mark.django_db
def test_search_form_accepts_iso_date(mock_search_provider):
    form = SearchForm(
        data={
            "provider": mock_search_provider.name,
            "study_date_from": "2025-02-14",
        }
    )
    assert form.is_valid()
    assert form.cleaned_data["study_date_from"] == datetime.date(2025, 2, 14)


@pytest.mark.django_db
def test_search_form_rejects_inverted_date_range(mock_search_provider):
    form = SearchForm(
        data={
            "provider": mock_search_provider.name,
            "study_date_from": "15/02/2025",
            "study_date_till": "14/02/2025",
        }
    )
    assert not form.is_valid()
    assert (
        "Study date from must be before study date till" in form.errors["__all__"]
    )


@pytest.mark.django_db
def test_search_form_round_trips_dates_when_re_rendered(mock_search_provider):
    Language.objects.create(code="en")
    Modality.objects.create(code="CT", filterable=True)

    form = SearchForm(
        data={
            "provider": mock_search_provider.name,
            "study_date_from": "14/02/2025",
            "study_date_till": "15/02/2025",
        }
    )
    assert form.is_valid()

    re_rendered = SearchForm(
        initial={
            "provider": form.cleaned_data["provider"],
            "study_date_from": form.cleaned_data["study_date_from"],
            "study_date_till": form.cleaned_data["study_date_till"],
        }
    )

    html = re_rendered.as_p()
    assert 'name="study_date_from"' in html and 'value="2025-02-14"' in html
    assert 'name="study_date_till"' in html and 'value="2025-02-15"' in html
