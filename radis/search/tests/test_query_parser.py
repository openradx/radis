# def stringify

from radis.search.utils.query_parser import QueryParser


def is_valid_query(query: str, final_query: str | None = None) -> bool:
    if final_query is None:
        final_query = query
    node, fixes = QueryParser().parse(query)
    assert len(fixes) == 0
    assert node is not None
    assert QueryParser.unparse(node) == final_query
    return True


def is_fixed_query(query: str, fixed_query: str, num_of_fixes: int) -> bool:
    node, fixes = QueryParser().parse(query)
    assert len(fixes) == num_of_fixes
    assert node is not None
    assert QueryParser.unparse(node) == fixed_query
    return True


def is_empty_query(query: str, num_of_fixes: int) -> bool:
    node, fixes = QueryParser().parse(query)
    assert len(fixes) == num_of_fixes
    assert node is None
    return True


def test_parse_valid_queries():
    # Words
    assert is_valid_query("foobar")
    assert is_valid_query("Hämatom")
    assert is_valid_query("Magen-Darm-Trakt")

    # Phrases
    assert is_valid_query('"foo bar"')
    assert is_valid_query('"foo ( bar"')
    assert is_valid_query('"foo \\" bar"')

    # Implicit AND operator
    assert is_valid_query("foo bar")

    # Explicit AND operator
    assert is_valid_query("foo AND bar")

    # OR operator
    assert is_valid_query("foo OR bar")

    # NOT operator
    assert is_valid_query("NOT foo")

    # Parentheses
    assert is_valid_query("(foo)")

    # More complex queries
    assert is_valid_query("foo AND bar OR baz")
    assert is_valid_query("foo AND (bar OR baz)")
    assert is_valid_query("foo AND (bar OR NOT baz)")
    assert is_valid_query('foo AND (bar OR NOT "baz qux") AND qux')
    assert is_valid_query('foo AND (NOT "bar baz" (moo OR zoo) AND yoo)')
    assert is_valid_query('foo AND (NOT "bar ( baz" (moo OR zoo) AND yoo)')


def test_shortened_queries():
    # Whitespace at start of line
    assert is_valid_query("   foo bar", "foo bar")

    # Whitespace at end of line
    assert is_valid_query("foo bar   ", "foo bar")

    # Redundant whitespace in between
    assert is_valid_query("foo   bar", "foo bar")

    # Whitespace after opening parenthesis
    assert is_valid_query("(   foo bar)", "(foo bar)")

    # Whitespace before closing parenthesis
    assert is_valid_query("(foo bar   )", "(foo bar)")

    # Don't fix whitespace in quoted terms
    assert is_valid_query('"foo   bar"')


def test_fixed_queries():
    # Fix unary operator before binary operator
    assert is_fixed_query("foo NOT AND bar", "foo NOT bar", 1)

    # Fix invalid consecutive operators
    assert is_fixed_query("foo AND AND bar", "foo AND bar", 1)
    assert is_fixed_query("foo AND OR bar", "foo AND bar", 1)
    assert is_fixed_query("foo AND OR AND bar", "foo AND bar", 1)
    assert is_fixed_query("foo AND OR NOT bar", "foo AND NOT bar", 1)
    assert is_fixed_query("foo AND NOT OR NOT bar", "foo AND NOT bar", 1)

    # Fix invalid binary operator at start of line
    assert is_fixed_query("AND foo bar", "foo bar", 1)
    assert is_fixed_query("OR foo bar", "foo bar", 1)

    # Fix invalid operator at end of line
    assert is_fixed_query("foo bar NOT", "foo bar", 1)
    assert is_fixed_query("foo bar AND", "foo bar", 1)
    assert is_fixed_query("foo bar OR", "foo bar", 1)

    # Fix invalid binary operator at start of parentheses expression
    assert is_fixed_query("(AND foo bar)", "(foo bar)", 1)
    assert is_fixed_query("(OR foo bar)", "(foo bar)", 1)

    # Fix invalid operator at end of parentheses expression
    assert is_fixed_query("(foo bar NOT)", "(foo bar)", 1)
    assert is_fixed_query("(foo bar AND)", "(foo bar)", 1)
    assert is_fixed_query("(foo bar OR)", "(foo bar)", 1)

    # Fix unbalanced parentheses
    assert is_fixed_query("(foo bar", "foo bar", 1)
    assert is_fixed_query("foo bar)", "foo bar", 1)

    # Fix unbalanced quotes
    assert is_fixed_query('"foo bar', "foo bar", 1)
    assert is_fixed_query('foo bar"', "foo bar", 1)
    assert is_fixed_query('"foo bar" baz"', '"foo bar" baz', 1)

    # Fix unvalid characters
    assert is_fixed_query("foo$bar", "foobar", 1)
    assert is_fixed_query("foo bar~ baz", "foo bar baz", 1)
    assert is_fixed_query('foo bar \\" baz', "foo bar baz", 1)

    # Multiple fixes
    assert is_fixed_query("foo \\) bar", "foo bar", 2)


def test_empty_queries():
    assert is_empty_query("", 0)
    assert is_empty_query("   ", 0)
    assert is_empty_query("()", 1)
    assert is_empty_query("(   )", 1)
    assert is_empty_query("( AND )", 2)
    assert is_empty_query("( AND OR )", 3)
    assert is_empty_query("AND OR )", 3)
