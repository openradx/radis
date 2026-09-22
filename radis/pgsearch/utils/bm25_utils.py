import re


def bm25_index_name(language_code: str) -> str:
    """Name of the per-language partial BM25 index on reports_report.

    One index per language because BM25 statistics live per index and a
    pg_textsearch index carries a single text_config; the language code is
    sanitized because it becomes part of a SQL identifier.
    """
    safe = re.sub(r"[^a-z0-9_]", "_", language_code.lower())
    return f"pgsearch_bm25_body_{safe}"
