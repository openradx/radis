import logging
import unicodedata
from functools import cache, lru_cache

import pycountry
from django.db import DatabaseError, connection

logger = logging.getLogger(__name__)


@lru_cache(maxsize=1)
def _get_available_search_configs_cached() -> set[str]:
    with connection.cursor() as cursor:
        # Static query with no user input.
        cursor.execute("SELECT cfgname FROM pg_ts_config")
        return {row[0].lower() for row in cursor.fetchall()}


def get_available_search_configs() -> set[str]:
    try:
        return _get_available_search_configs_cached()
    except DatabaseError as exc:
        logger.error(
            "Failed to read pg_ts_config; falling back to simple. %s",
            exc,
            exc_info=True,
        )
        return set()


def _normalize_language_name(name: str) -> list[str]:
    """Normalize language names for config matching by stripping diacritics and punctuation."""
    trimmed = name.split("(", 1)[0].strip()
    if not trimmed:
        return []
    # NFKD decomposes characters so diacritics can be removed consistently.
    normalized = unicodedata.normalize("NFKD", trimmed)
    normalized = "".join(char for char in normalized if not unicodedata.combining(char))
    normalized = "".join(char if char.isalnum() else " " for char in normalized)
    normalized = " ".join(normalized.strip().lower().split())
    if not normalized:
        return []
    tokens = normalized.split()
    candidates = {normalized.replace(" ", "_")}
    candidates.update(tokens)
    return list(candidates)


def _language_name_candidates(code: str) -> list[str]:
    """Resolve ISO codes to candidate config names via pycountry, with safe fallbacks."""
    language = None
    if len(code) == 2:
        language = pycountry.languages.get(alpha_2=code)
    elif len(code) == 3:
        language = pycountry.languages.get(alpha_3=code)
    if language is None:
        try:
            language = pycountry.languages.lookup(code)
        except LookupError:
            return []
    names: list[str] = []
    for attr in ("name", "common_name", "inverted_name"):
        value = getattr(language, attr, None)
        if value:
            names.append(value)
    candidates: list[str] = []
    for name in names:
        candidates.extend(_normalize_language_name(name))
    return candidates


def _is_safe_language_code(code: str) -> bool:
    return all(char.isalnum() or char in {"-", "_"} for char in code)


def clear_search_config_cache() -> None:
    _get_available_search_configs_cached.cache_clear()
    code_to_language.cache_clear()


@cache
def code_to_language(code: str) -> str:
    """Resolve a language code to its Postgres text-search configuration.

    Cached because ``_language_configs`` (radis.pgsearch.providers) now calls
    this once per distinct ``Language`` row on every search, not once per
    request: an unresolvable code falls through to ``pycountry.languages.lookup``
    (a linear scan, ~1ms), and orphaned ``Language`` rows -- RADIS accepts
    arbitrary codes over the API, and a row can outlive the reports that used
    it -- accumulate over time, so that cost would otherwise be paid on every
    search, for every such row, forever. A pure function of ``code`` plus the
    already-cached config set (see ``get_available_search_configs``), so this
    is safe to cache the same way; ``clear_search_config_cache`` invalidates
    both together.

    Side effect of caching: the "Unknown language code ... falling back to
    simple" WARNING below only fires the first time a given unresolvable code
    is seen per process, not once per call -- like the precedent in
    ``_LOGGED_PERMANENT_FAILURE_CONFIGS`` (radis.pgsearch.providers), this is
    probably desirable, but an operator who sees the warning once should not
    assume the code was only looked up once.
    """
    if not code:
        return "simple"
    if not _is_safe_language_code(code):
        logger.debug("Invalid language code '%s'; falling back to simple.", code)
        return "simple"
    normalized = code.lower()
    base = normalized.split("-", 1)[0].split("_", 1)[0]
    configs = get_available_search_configs()
    seen: set[str] = set()
    for candidate in (normalized, base, *_language_name_candidates(base)):
        if candidate in seen:
            continue
        seen.add(candidate)
        if candidate in configs:
            return candidate
    logger.warning(
        "Unknown language code '%s' (normalized '%s'); falling back to simple.",
        code,
        base,
    )
    return "simple"
