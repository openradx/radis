import re

# Matches  label:word  or  label:"two words"
_LABEL_RE = re.compile(r'label:(?:"([^"]+)"|(\S+))')


def extract_label_filters(query: str) -> tuple[str, list[str]]:
    """Pull `label:<name>` tokens out of a raw query string.

    Returns (remaining_query, label_names). `label:"a b"` supports spaces via quotes.
    The remaining query has the label tokens removed and surrounding whitespace collapsed.
    A bare ``label:`` with no immediately-following value is left untouched in the remaining query.
    """
    labels: list[str] = []

    def _collect(match: re.Match) -> str:
        labels.append(match.group(1) or match.group(2))
        return " "

    remaining = _LABEL_RE.sub(_collect, query)
    remaining = re.sub(r"\s+", " ", remaining).strip()
    return remaining, labels
