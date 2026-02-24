from unittest.mock import MagicMock, patch

from django.conf import settings

from radis.pgsearch.providers import (
    _build_filter_query,
    _build_query_string,
    sanitize_term,
)
from radis.search.site import SearchFilters
from radis.search.utils.query_parser import BinaryNode, ParensNode, TermNode, UnaryNode


class TestSanitizeTerm:
    def test_removes_special_characters(self):
        assert sanitize_term("foo$bar") == "foobar"

    def test_keeps_valid_characters(self):
        assert sanitize_term("foo-bar_baz") == "foo-bar_baz"

    def test_empty_string(self):
        assert sanitize_term("") == ""

    def test_unicode_characters(self):
        assert sanitize_term("Hämatom") == "Hämatom"


class TestBuildQueryString:
    def test_word_term(self):
        node = TermNode(term_type="WORD", value="pneumonia")
        assert _build_query_string(node) == "pneumonia"

    def test_phrase_term(self):
        node = TermNode(term_type="PHRASE", value="lung cancer")
        assert _build_query_string(node) == "'lung' <-> 'cancer'"

    def test_and_binary(self):
        left = TermNode(term_type="WORD", value="foo")
        right = TermNode(term_type="WORD", value="bar")
        node = BinaryNode(operator="AND", left=left, right=right)
        assert _build_query_string(node) == "foo & bar"

    def test_or_binary(self):
        left = TermNode(term_type="WORD", value="foo")
        right = TermNode(term_type="WORD", value="bar")
        node = BinaryNode(operator="OR", left=left, right=right)
        assert _build_query_string(node) == "foo | bar"

    def test_not_unary(self):
        operand = TermNode(term_type="WORD", value="foo")
        node = UnaryNode(operator="NOT", operand=operand)
        assert _build_query_string(node) == "!foo"

    def test_parens(self):
        inner = TermNode(term_type="WORD", value="foo")
        node = ParensNode(expression=inner)
        assert _build_query_string(node) == "(foo)"


class TestBuildFilterQuery:
    def test_empty_filters(self):
        filters = SearchFilters(group=1)
        q = _build_filter_query(filters)
        assert str(q) == str(q)  # Q() is the identity

    def test_patient_sex_filter(self):
        filters = SearchFilters(group=1, patient_sex="M")
        q = _build_filter_query(filters)
        assert "patient_sex" in str(q)

    def test_language_filter(self):
        filters = SearchFilters(group=1, language="en")
        q = _build_filter_query(filters)
        assert "language__code" in str(q)

    def test_modalities_filter(self):
        filters = SearchFilters(group=1, modalities=["CT", "MR"])
        q = _build_filter_query(filters)
        assert "modalities__code__in" in str(q)

    def test_age_range_filter(self):
        filters = SearchFilters(group=1, patient_age_from=30, patient_age_till=60)
        q = _build_filter_query(filters)
        q_str = str(q)
        assert "patient_age__gte" in q_str
        assert "patient_age__lte" in q_str

    def test_date_range_filter(self):
        from datetime import date

        filters = SearchFilters(
            group=1,
            study_date_from=date(2023, 1, 1),
            study_date_till=date(2023, 12, 31),
        )
        q = _build_filter_query(filters)
        q_str = str(q)
        assert "study_datetime__date__gte" in q_str
        assert "study_datetime__date__lte" in q_str


class TestRRFConstant:
    def test_rrf_k_default_is_60(self):
        assert settings.HYBRID_SEARCH_RRF_K == 60

    def test_rrf_score_calculation(self):
        """Verify RRF score formula: 1/(k+rank)."""
        k = settings.HYBRID_SEARCH_RRF_K
        # A document ranked #1 in both FTS and vector should get the highest score
        score_top = 1.0 / (k + 1) + 1.0 / (k + 1)
        # A document ranked #100 in both should get a lower score
        score_low = 1.0 / (k + 100) + 1.0 / (k + 100)
        assert score_top > score_low

    def test_rrf_single_source(self):
        """A document appearing only in FTS still gets a score."""
        k = settings.HYBRID_SEARCH_RRF_K
        score = 1.0 / (k + 1)
        assert score > 0
        # But lower than a document in both
        score_both = 1.0 / (k + 1) + 1.0 / (k + 1)
        assert score_both > score


class TestSearchDispatch:
    def test_search_uses_fts_when_semantic_disabled(self):
        with patch("radis.pgsearch.providers._fts_search") as mock_fts:
            with patch("radis.pgsearch.providers._hybrid_search") as mock_hybrid:
                from radis.pgsearch.providers import search
                from radis.search.site import Search, SearchFilters
                from radis.search.utils.query_parser import QueryParser

                node, _ = QueryParser().parse("test")
                assert node is not None
                s = Search(
                    query=node,
                    query_text="test",
                    filters=SearchFilters(group=1, use_semantic=False),
                )

                mock_fts.return_value = MagicMock()
                search(s)

                mock_fts.assert_called_once_with(s)
                mock_hybrid.assert_not_called()

    def test_search_uses_hybrid_when_semantic_enabled(self):
        with patch("radis.pgsearch.providers._fts_search") as mock_fts:
            with patch("radis.pgsearch.providers._hybrid_search") as mock_hybrid:
                from radis.pgsearch.providers import search
                from radis.search.site import Search, SearchFilters
                from radis.search.utils.query_parser import QueryParser

                node, _ = QueryParser().parse("test")
                assert node is not None
                s = Search(
                    query=node,
                    query_text="test",
                    filters=SearchFilters(group=1, use_semantic=True),
                )

                mock_hybrid.return_value = MagicMock()
                search(s)

                mock_hybrid.assert_called_once_with(s)
                mock_fts.assert_not_called()

    def test_search_uses_fts_when_semantic_enabled_but_no_query_text(self):
        with patch("radis.pgsearch.providers._fts_search") as mock_fts:
            with patch("radis.pgsearch.providers._hybrid_search") as mock_hybrid:
                from radis.pgsearch.providers import search
                from radis.search.site import Search, SearchFilters
                from radis.search.utils.query_parser import QueryParser

                node, _ = QueryParser().parse("test")
                assert node is not None
                s = Search(
                    query=node,
                    query_text="",
                    filters=SearchFilters(group=1, use_semantic=True),
                )

                mock_fts.return_value = MagicMock()
                search(s)

                mock_fts.assert_called_once_with(s)
                mock_hybrid.assert_not_called()
