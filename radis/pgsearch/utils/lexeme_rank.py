"""Impact-ordered lexeme rank support: the single-term FTS ranking fast path.

`pgsearch_lexemerank` stores, for every report and every lexeme of its
tsvector, the exact ``ts_rank`` value the FTS arm computes for a single-term
query of that lexeme. Ordering by the precomputed rank under the btree over
``(lexeme, rank DESC)`` turns "top N reports for one word" into a bounded
index walk instead of a ``ts_rank`` call over every matching row.

This deliberately covers *only* single-word queries. A multi-term score is a
per-query aggregate (the sum of the terms' contributions over the documents
matching the boolean expression); it is not stored anywhere and can only be
produced by intersecting/uniting the per-term posting lists and sorting the
combination, which costs nearly as much as ``ts_rank`` itself. Early
termination over several impact-ordered lists (WAND-style pruning with
per-term score ceilings) is a procedural index traversal that SQL cannot
express, so multi-term queries keep the ``ts_rank`` path.

The table is maintained by a row trigger on the search-index table, installed
by ``manage.py sync_lexeme_ranks`` rather than by a migration: the feature is
opt-in (``HYBRID_FTS_LEXEME_RANK_INDEX``) and the trigger roughly doubles the
write cost of (re)indexing a report, which deployments that leave the flag
off must not pay.
"""

import logging
import re

from django.db import connection

from ..models import LexemeRank, ReportSearchIndex

logger = logging.getLogger(__name__)

TRIGGER_NAME = "pgsearch_lexemerank_sync"
FUNCTION_NAME = "pgsearch_lexemerank_sync"

# Renders u.lexeme as a single-quoted tsquery literal ('fraktur' -> 'fraktur',
# doubling embedded quotes). The text-to-tsquery cast performs no
# normalization, so the stored rank is computed against the exact lexeme the
# tsvector contains -- the same tsquery a single-word search produces after
# to_tsquery() has stemmed the word under the document's own configuration.
_QUOTED_LEXEME_SQL = "('''' || replace(u.lexeme, '''', '''''') || '''')::tsquery"

_BACKFILL_SQL_TEMPLATE = """
INSERT INTO {lexeme_table} (report_id, lexeme, rank)
SELECT v.report_id, u.lexeme, ts_rank(v.search_vector, {quoted_lexeme})
FROM {index_table} v
CROSS JOIN LATERAL unnest(v.search_vector) AS u
WHERE v.id BETWEEN %s AND %s
  AND v.search_vector IS NOT NULL
  AND NOT EXISTS (SELECT 1 FROM {lexeme_table} lr WHERE lr.report_id = v.report_id)
ON CONFLICT DO NOTHING
"""


def install_trigger(cursor) -> None:
    lexeme_table = LexemeRank._meta.db_table
    index_table = ReportSearchIndex._meta.db_table
    cursor.execute(
        f"""
CREATE OR REPLACE FUNCTION {FUNCTION_NAME}() RETURNS trigger AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        DELETE FROM {lexeme_table} WHERE report_id = OLD.report_id;
        RETURN OLD;
    END IF;
    DELETE FROM {lexeme_table} WHERE report_id = NEW.report_id;
    IF NEW.search_vector IS NOT NULL THEN
        INSERT INTO {lexeme_table} (report_id, lexeme, rank)
        SELECT NEW.report_id, u.lexeme, ts_rank(NEW.search_vector, {_QUOTED_LEXEME_SQL})
        FROM unnest(NEW.search_vector) AS u;
    END IF;
    RETURN NEW;
END
$$ LANGUAGE plpgsql
"""
    )
    # UPDATE OF search_vector keeps embedding-only writes (the vector backfill)
    # from pointlessly recomputing unchanged lexeme ranks; INSERT and DELETE
    # always fire.
    cursor.execute(
        f"""
CREATE OR REPLACE TRIGGER {TRIGGER_NAME}
AFTER INSERT OR UPDATE OF search_vector OR DELETE ON {index_table}
FOR EACH ROW EXECUTE FUNCTION {FUNCTION_NAME}()
"""
    )


def remove_trigger(cursor) -> None:
    index_table = ReportSearchIndex._meta.db_table
    cursor.execute(f"DROP TRIGGER IF EXISTS {TRIGGER_NAME} ON {index_table}")
    cursor.execute(f"DROP FUNCTION IF EXISTS {FUNCTION_NAME}()")


def trigger_installed(cursor) -> bool:
    cursor.execute(
        "SELECT 1 FROM pg_trigger WHERE tgname = %s AND NOT tgisinternal", [TRIGGER_NAME]
    )
    return cursor.fetchone() is not None


# Process-local memo of trigger_installed(). The fast path must never run
# against a table nobody maintains (missing rows would silently shrink the FTS
# arm), and checking pg_trigger on every search would tax the hot path.
# Installation happens via a management command plus process restarts, so a
# once-per-process probe is accurate enough.
_ready: bool | None = None


def lexeme_rank_ready() -> bool:
    global _ready
    if _ready is None:
        with connection.cursor() as cursor:
            installed = trigger_installed(cursor)
        if not installed:
            logger.warning(
                "HYBRID_FTS_LEXEME_RANK_INDEX is enabled but the %s trigger is not "
                "installed; run `manage.py sync_lexeme_ranks`. Serving single-term "
                "queries through the ts_rank path until then.",
                TRIGGER_NAME,
            )
        _ready = installed
    return _ready


def reset_ready_cache() -> None:
    global _ready
    _ready = None


_SINGLE_LEXEME_RE = re.compile(r"^'((?:[^']|'')+)'$")


def resolve_single_lexeme(config: str, quoted_term: str) -> tuple[str, str | None]:
    """How ``quoted_term`` (a ``'word'`` raw-tsquery token) normalizes under
    ``config``.

    Returns ``("one", lexeme)`` when it becomes exactly one plain lexeme -- the
    shape the lexeme-rank table can serve. ``("none", None)`` means the word
    normalized away entirely (a stopword): the raw tsquery matches nothing
    under this configuration. ``("many", None)`` covers everything else
    (compound-splitting dictionaries, prefixes -- shapes the table cannot
    answer), which callers must treat as "fall back to ts_rank".
    """
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT q::text, numnode(q) FROM to_tsquery(%s::regconfig, %s) AS q",
            [config, quoted_term],
        )
        row = cursor.fetchone()
    if row is None:
        return ("many", None)
    text, numnode = row
    if numnode == 0:
        return ("none", None)
    if numnode > 1:
        return ("many", None)
    match = _SINGLE_LEXEME_RE.match(text)
    if match is None:
        return ("many", None)
    return ("one", match.group(1).replace("''", "'"))


def backfill_lexeme_ranks(batch_size: int, on_batch=None) -> int:
    """Fill lexeme ranks for search-index rows that have none yet.

    Walks the search-index table in id ranges so each INSERT stays a bounded
    transaction-friendly chunk. Rows for reports that already have any lexeme
    ranks are skipped -- the trigger keeps those fresh -- so re-running after
    an interruption resumes instead of redoing work. Returns the number of
    lexeme rank rows inserted.
    """
    lexeme_table = LexemeRank._meta.db_table
    index_table = ReportSearchIndex._meta.db_table
    sql = _BACKFILL_SQL_TEMPLATE.format(
        lexeme_table=lexeme_table, index_table=index_table, quoted_lexeme=_QUOTED_LEXEME_SQL
    )
    total = 0
    with connection.cursor() as cursor:
        cursor.execute(f"SELECT min(id), max(id) FROM {index_table}")
        row = cursor.fetchone()
        if row is None or row[0] is None:
            return 0
        low, high = row
        start = low
        while start <= high:
            end = start + batch_size - 1
            cursor.execute(sql, [start, end])
            total += cursor.rowcount
            if on_batch is not None:
                on_batch(min(end, high) - low + 1, high - low + 1, total)
            start = end + 1
    return total
