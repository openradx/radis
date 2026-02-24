import pytest

from radis.reports.factories import LanguageFactory
from radis.search.forms import SearchForm


@pytest.mark.django_db
class TestSearchFormSemanticField:
    def test_semantic_field_exists(self):
        LanguageFactory.create(code="en")
        form = SearchForm()
        assert "semantic" in form.fields

    def test_semantic_field_not_required(self):
        LanguageFactory.create(code="en")
        form = SearchForm()
        assert form.fields["semantic"].required is False

    def test_semantic_checked(self):
        LanguageFactory.create(code="en")
        form = SearchForm(data={"semantic": "on"})
        assert form.is_valid()
        assert form.cleaned_data["semantic"] is True

    def test_semantic_unchecked(self):
        LanguageFactory.create(code="en")
        form = SearchForm(data={})
        assert form.is_valid()
        assert form.cleaned_data["semantic"] is False
