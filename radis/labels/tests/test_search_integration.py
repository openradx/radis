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
        from radis.reports.models import Report
        from radis.search.site import SearchFilters
        from radis.pgsearch.providers import _build_filter_query

        q = _build_filter_query(SearchFilters(group=1, labels=["pneumonia"]))
        matched = set(Report.objects.filter(q).values_list("id", flat=True))
        assert labeled_corpus["r1"].id in matched   # YES
        assert labeled_corpus["r2"].id in matched   # MAYBE
        assert labeled_corpus["r3"].id not in matched

    def test_and_across_labels(self, labeled_corpus):
        from radis.reports.models import Report
        from radis.search.site import SearchFilters
        from radis.pgsearch.providers import _build_filter_query

        q = _build_filter_query(SearchFilters(group=1, labels=["pneumonia", "effusion"]))
        # r1: YES pneumonia, NO effusion → excluded
        # r2: MAYBE pneumonia, no effusion answer → excluded
        # r3: no pneumonia answer, YES effusion → excluded
        assert list(Report.objects.filter(q).values_list("id", flat=True)) == []


class TestFacetCounts:
    def test_counts(self, labeled_corpus):
        from radis.reports.models import Report
        from radis.pgsearch.providers import facet_label_counts

        rqs = Report.objects.all()
        d = dict(facet_label_counts(rqs, top_n=10))
        assert d.get("pneumonia") == 2
        assert d.get("effusion") == 1

    def test_top_n(self, labeled_corpus):
        from radis.reports.models import Report
        from radis.pgsearch.providers import facet_label_counts

        assert len(facet_label_counts(Report.objects.all(), top_n=1)) == 1
