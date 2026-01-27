from django.db import DatabaseError

from radis.pgsearch.utils import language_utils
from radis.pgsearch.utils.language_utils import (
    _is_safe_language_code,
    _normalize_language_name,
    code_to_language,
    get_available_search_configs,
)


def set_configs(monkeypatch, configs):
    monkeypatch.setattr(language_utils, "get_available_search_configs", lambda: configs)


def test_code_to_language_known_languages(monkeypatch):
    set_configs(monkeypatch, {"english", "german", "simple"})
    assert code_to_language("en") == "english"
    assert code_to_language("de") == "german"


def test_code_to_language_case_insensitive(monkeypatch):
    set_configs(monkeypatch, {"english", "german", "simple"})
    assert code_to_language("EN") == "english"
    assert code_to_language("De") == "german"


def test_code_to_language_locale_variants(monkeypatch):
    set_configs(monkeypatch, {"english", "simple"})
    assert code_to_language("en-US") == "english"
    assert code_to_language("en_GB") == "english"


def test_code_to_language_name_match(monkeypatch):
    set_configs(monkeypatch, {"turkish", "simple"})
    assert code_to_language("tr") == "turkish"
    assert code_to_language("turkish") == "turkish"


def test_code_to_language_multiword_match(monkeypatch):
    set_configs(monkeypatch, {"norwegian", "simple"})
    assert code_to_language("nb") == "norwegian"


def test_code_to_language_unknown_fallback(monkeypatch):
    set_configs(monkeypatch, {"english", "german", "simple"})
    assert code_to_language("tr") == "simple"
    assert code_to_language("xx-YY") == "simple"


def test_code_to_language_empty(monkeypatch):
    set_configs(monkeypatch, {"english", "german", "simple"})
    assert code_to_language("") == "simple"


def test_code_to_language_three_letter_code(monkeypatch):
    set_configs(monkeypatch, {"german", "simple"})
    assert code_to_language("deu") == "german"


def test_code_to_language_invalid_chars(monkeypatch):
    set_configs(monkeypatch, {"english", "simple"})
    assert code_to_language("en;DROP") == "simple"


def test_code_to_language_continents(monkeypatch):
    set_configs(
        monkeypatch,
        {
            "polish",
            "japanese",
            "chinese",
            "swahili",
            "hausa",
            "simple",
        },
    )
    assert code_to_language("pl") == "polish"
    assert code_to_language("ja") == "japanese"
    assert code_to_language("zh") == "chinese"
    assert code_to_language("sw") == "swahili"
    assert code_to_language("ha") == "hausa"


def test_normalize_language_name_diacritics():
    candidates = _normalize_language_name("Arbëreshë Albanian")
    assert "arbereshe_albanian" in candidates
    assert "arbereshe" in candidates
    assert "albanian" in candidates


def test_normalize_language_name_parentheses():
    candidates = _normalize_language_name("Chinese (Traditional)")
    assert "chinese" in candidates


def test_language_code_validation():
    assert _is_safe_language_code("en")
    assert _is_safe_language_code("en-US")
    assert not _is_safe_language_code("en;DROP")


def test_get_available_search_configs_db_error(monkeypatch):
    class FailingCursor:
        def __enter__(self):
            raise DatabaseError("boom")

        def __exit__(self, exc_type, exc, tb):
            return False

    language_utils._get_available_search_configs_cached.cache_clear()
    monkeypatch.setattr(language_utils.connection, "cursor", lambda: FailingCursor())
    assert get_available_search_configs() == set()


def test_get_available_search_configs_db_error_not_cached(monkeypatch):
    class FailingCursor:
        def __enter__(self):
            raise DatabaseError("boom")

        def __exit__(self, exc_type, exc, tb):
            return False

    class WorkingCursor:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def execute(self, _query):
            return None

        def fetchall(self):
            return [("english",)]

    language_utils._get_available_search_configs_cached.cache_clear()
    monkeypatch.setattr(language_utils.connection, "cursor", lambda: FailingCursor())
    assert get_available_search_configs() == set()
    monkeypatch.setattr(language_utils.connection, "cursor", lambda: WorkingCursor())
    assert get_available_search_configs() == {"english"}
