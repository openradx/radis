import logging
import unicodedata
from functools import lru_cache

import pycountry
from django.db import DatabaseError, connection

logger = logging.getLogger(__name__)


@lru_cache(maxsize=1)
def _get_available_search_configs_cached() -> set[str]:
    with connection.cursor() as cursor:
        cursor.execute("SELECT cfgname FROM pg_ts_config")
        return {row[0].lower() for row in cursor.fetchall()}


def get_available_search_configs() -> set[str]:
    try:
        return _get_available_search_configs_cached()
    except DatabaseError as exc:
        logger.warning("Failed to read pg_ts_config; falling back to simple. %s", exc)
        return set()


def _normalize_language_name(name: str) -> list[str]:
    trimmed = name.split("(", 1)[0].strip()
    if not trimmed:
        return []
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
    get_available_search_configs.cache_clear()


def code_to_language(code: str) -> str:
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
