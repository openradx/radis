"""Unit tests for the negation extractor that enforces `NOT` branches on the
vector-candidate queryset (providers._build_negative_query_string)."""

import pytest

from radis.pgsearch.providers import _build_negative_query_string
from radis.search.utils.query_parser import QueryParser


def _neg(query_str: str) -> str:
    node, _ = QueryParser().parse(query_str)
    assert node is not None
    return _build_negative_query_string(node)


def test_positive_only_query_has_no_negation():
    assert _neg("pneumothorax") == ""


def test_implicit_and_positive_terms_have_no_negation():
    assert _neg("pneumothorax effusion") == ""


def test_and_not_extracts_the_negated_term():
    assert _neg("pneumothorax AND NOT effusion") == "('effusion')"


def test_multiple_top_level_negations_are_ored():
    result = _neg("pneumothorax AND NOT effusion AND NOT fracture")
    assert result == "('effusion') | ('fracture')"


def test_negation_under_or_is_not_extracted():
    # Branch-scoped: excluding effusion globally would drop legitimate C-branch
    # hits, so the OR-nested NOT is left as the documented residual.
    assert _neg("(pneumothorax AND NOT effusion) OR fracture") == ""


def test_negated_group_excludes_the_whole_group():
    assert _neg("pneumothorax AND NOT (effusion OR fracture)") == "(('effusion' | 'fracture'))"


@pytest.mark.parametrize("query_str", ["NOT effusion", "NOT (effusion OR fracture)"])
def test_pure_negation_still_extracts_but_vector_half_is_skipped_upstream(query_str):
    # The extractor still yields the exclusion; the provider never applies it
    # because a pure-NOT query embeds to "" and skips the vector half entirely.
    assert _neg(query_str) != ""
