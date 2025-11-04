import datetime

import pytest

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
