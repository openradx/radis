import pytest

from radis.search.utils.query_parser import QueryParser


@pytest.mark.parametrize(
    "query,expected",
    [
        # Simple positive term — unchanged.
        ("pneumothorax", "pneumothorax"),
        # Phrase preserved with quotes.
        ('"chest x-ray"', '"chest x-ray"'),
        # Implicit AND (no operator) — both sides survive.
        ("cardiac arrest", "cardiac arrest"),
        # Explicit AND — both sides survive, operator preserved.
        ("A AND B", "A AND B"),
        # OR — both sides survive, operator preserved.
        ("A OR B", "A OR B"),
        # NOT alone — empty.
        ("NOT pneumothorax", ""),
        # AND NOT — left survives, NOT branch dropped, AND collapses.
        ("A AND NOT B", "A"),
        # NOT AND — right survives, NOT branch dropped, AND collapses.
        ("NOT A AND B", "B"),
        # NOT OR NOT — both branches dropped, empty.
        ("NOT A OR NOT B", ""),
        # Mixed: AND OR with a NOT branch — surviving structure retained.
        ("(A AND NOT B) OR C", "(A) OR C"),
        # Nested NOT inside parens — empty parens collapsed.
        ("A AND (NOT B)", "A"),
        # Double-nested OR with one NOT — only NOT branch dropped.
        ("(A OR B) AND NOT C", "(A OR B)"),
    ],
)
def test_unparse_for_embedding(query, expected):
    node, _fixes = QueryParser().parse(query)
    assert node is not None, f"parser produced empty node for {query!r}"
    assert QueryParser.unparse_for_embedding(node) == expected
