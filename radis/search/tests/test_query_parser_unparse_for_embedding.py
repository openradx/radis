import pytest

from radis.search.utils.query_parser import QueryParser


@pytest.mark.parametrize(
    "query,expected",
    [
        # Simple positive term — unchanged.
        ("pneumothorax", "pneumothorax"),
        # Phrase: quotes dropped, value preserved (embedding tokenizers handle
        # multi-word spans natively; the quote chars are noise).
        ('"chest x-ray"', "chest x-ray"),
        # Implicit AND (no operator) — both sides survive, joined by a space.
        ("cardiac arrest", "cardiac arrest"),
        # Explicit AND — operator token dropped; bag of terms.
        ("A AND B", "A B"),
        # Explicit OR — operator token dropped; bag of terms.
        ("A OR B", "A B"),
        # NOT alone — empty (polarity-blind for negation).
        ("NOT pneumothorax", ""),
        # AND NOT — left survives, NOT branch dropped, AND collapses.
        ("A AND NOT B", "A"),
        # NOT AND — right survives, NOT branch dropped, AND collapses.
        ("NOT A AND B", "B"),
        # NOT OR NOT — both branches dropped, empty.
        ("NOT A OR NOT B", ""),
        # Mixed AND OR with a NOT branch — grouping parens dropped,
        # operators dropped, surviving terms joined.
        ("(A AND NOT B) OR C", "A C"),
        # Nested NOT inside parens — empty parens collapsed.
        ("A AND (NOT B)", "A"),
        # Double-nested OR with one NOT — parens + operators dropped,
        # surviving disjunction terms joined.
        ("(A OR B) AND NOT C", "A B"),
    ],
)
def test_unparse_for_embedding(query, expected):
    node, _fixes = QueryParser().parse(query)
    assert node is not None, f"parser produced empty node for {query!r}"
    assert QueryParser.unparse_for_embedding(node) == expected
