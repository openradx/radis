import logging
import unicodedata
from functools import lru_cache

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


# Bounded: the API creates a Language row for any code it is sent
# (reports/api/serializers.py), so an unbounded cache would grow with them.
@lru_cache(maxsize=1024)
def code_to_language(code: str) -> str:
    """Resolve a language code to its Postgres text-search configuration.

    Cached because ``_language_configs`` calls this once per ``Language`` row
    on every search, and an unresolvable code costs a ``pycountry`` linear scan
    (~1ms). Invalidated together with the config set it depends on by
    ``clear_search_config_cache``.

    Side effect: the unknown-code WARNING below fires once per process per
    code, not once per call, so seeing it once does not mean one lookup.
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
