from unittest.mock import patch

import pytest

from radis.labels.factories import AnswerFactory, QuestionFactory
from radis.reports.factories import ReportFactory


@pytest.fixture
def labeled_corpus():
    r1, r2, r3 = ReportFactory(), ReportFactory(), ReportFactory()
    q_pneu = QuestionFactory(label="pneumonia", group="lung")
    q_eff = QuestionFactory(label="effusion", group="lung")
    AnswerFactory(report=r1, question=q_pneu, value="YES")
    AnswerFactory(report=r1, question=q_eff, value="NO")
    AnswerFactory(report=r2, question=q_pneu, value="MAYBE")
    AnswerFactory(report=r3, question=q_eff, value="YES")
    return {"r1": r1, "r2": r2, "r3": r3}


class TestSearchFiltersCarryLabels:
    def test_labels_field_default_empty(self):
        from radis.search.site import SearchFilters

        assert SearchFilters(group=1).labels == []

    def test_labels_roundtrips(self):
        from radis.search.site import SearchFilters

        assert SearchFilters(group=1, labels=["pneumonia"]).labels == ["pneumonia"]


class TestLabelFilterTranslation:
    def test_single_label_filter(self, labeled_corpus):
        from radis.pgsearch.models import ReportSearchVector
        from radis.pgsearch.providers import _build_filter_query
        from radis.search.site import SearchFilters

        q = _build_filter_query(SearchFilters(group=1, labels=["pneumonia"]))
        matched_report_ids = set(
            ReportSearchVector.objects.filter(q).values_list("report_id", flat=True)
        )
        assert labeled_corpus["r1"].id in matched_report_ids  # YES
        assert labeled_corpus["r2"].id in matched_report_ids  # MAYBE
        assert labeled_corpus["r3"].id not in matched_report_ids

    def test_and_across_labels(self, labeled_corpus):
        from radis.pgsearch.models import ReportSearchVector
        from radis.pgsearch.providers import _build_filter_query
        from radis.search.site import SearchFilters

        q = _build_filter_query(SearchFilters(group=1, labels=["pneumonia", "effusion"]))
        # r1: YES pneumonia, NO effusion → excluded
        # r2: MAYBE pneumonia, no effusion answer → excluded
        # r3: no pneumonia answer, YES effusion → excluded
        assert list(ReportSearchVector.objects.filter(q).values_list("report_id", flat=True)) == []


class TestSearchViewPipesLabels:
    """Regression: SearchView must forward form.cleaned_data['labels'] into SearchFilters.

    These tests bypass the full Django request stack (debug-toolbar middleware in the
    development settings makes a real client.get() brittle) and instead drive the view's
    ``get`` method directly so we can capture the SearchFilters handed to the provider.
    """

    @pytest.mark.django_db
    def test_get_constructs_search_filters_with_labels_from_form(self):
        from typing import cast
        from unittest.mock import MagicMock

        from adit_radis_shared.accounts.factories import GroupFactory, UserFactory
        from adit_radis_shared.common.types import AuthenticatedHttpRequest
        from django.test import RequestFactory

        from radis.search.site import SearchProvider, SearchResult
        from radis.search.views import SearchView

        QuestionFactory(label="pneumonia", group="lung", active=True)

        user = UserFactory.create(is_active=True)
        group = GroupFactory.create()
        user.groups.add(group)
        user.active_group = group
        user.save()

        captured = {}

        def mock_search(search):
            captured["filters"] = search.filters
            return SearchResult(total_count=0, total_relation="exact", documents=[])

        provider = SearchProvider(name="Test", search=mock_search, max_results=1000)

        wsgi_request = RequestFactory().get(
            "/search/", {"query": "anything", "labels": ["pneumonia"]}
        )
        wsgi_request.user = user
        request = cast(AuthenticatedHttpRequest, wsgi_request)

        view = SearchView()
        view.request = request

        # Stub out render so we don't depend on template/middleware behaviour.
        with (
            patch("radis.search.views.search_provider", provider),
            patch("radis.search.views.render", return_value=MagicMock()),
        ):
            view.get(request)

        assert "filters" in captured, "search provider was not invoked"
        assert captured["filters"].labels == ["pneumonia"]

    @pytest.mark.django_db
    def test_form_exposes_labels_in_cleaned_data(self):
        """Sanity check: the form actually puts labels into cleaned_data."""
        from radis.search.forms import SearchForm

        QuestionFactory(label="pneumonia", group="lung", active=True)

        form = SearchForm({"query": "x", "labels": ["pneumonia"]})
        assert form.is_valid(), form.errors
        assert form.cleaned_data["labels"] == ["pneumonia"]


class TestFacetCounts:
    def test_counts(self, labeled_corpus):
        from radis.pgsearch.providers import facet_label_counts
        from radis.reports.models import Report

        rqs = Report.objects.all()
        d = dict(facet_label_counts(rqs, top_n=10))
        assert d.get("pneumonia") == 2
        assert d.get("effusion") == 1

    def test_top_n(self, labeled_corpus):
        from radis.pgsearch.providers import facet_label_counts
        from radis.reports.models import Report

        assert len(facet_label_counts(Report.objects.all(), top_n=1)) == 1
