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
