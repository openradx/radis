"""Unit tests for the shared CSV export helpers."""

import pytest

from radis.core.utils.csv_export import escape_formula, format_cell


@pytest.mark.parametrize(
    "value",
    [
        '=HYPERLINK("http://evil")',
        "+1234",
        "-1234",
        "@cmd",
        "\tvalue",
        "\rvalue",
        # Some spreadsheet importers trim leading whitespace before deciding
        # whether a cell is a formula, so whitespace-cloaked markers must be
        # escaped as well.
        ' =HYPERLINK("http://evil")',
        "  +1",
        "\t=1+1",
        " \r=1+1",
    ],
)
def test_escape_formula_prefixes_dangerous_values(value):
    assert escape_formula(value) == "'" + value


@pytest.mark.parametrize(
    "value",
    [
        "",
        "plain text",
        "1234",
        " leading space but harmless",
        "no = at start",
    ],
)
def test_escape_formula_leaves_benign_values_untouched(value):
    assert escape_formula(value) == value


def test_format_cell_handles_none_bool_and_escaping():
    assert format_cell(None) == ""
    assert format_cell(True) == "yes"
    assert format_cell(False) == "no"
    assert format_cell(" =1+1") == "' =1+1"
    assert format_cell(42) == "42"
