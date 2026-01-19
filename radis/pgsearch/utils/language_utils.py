LANGUAGES = {
    "de": "german",
    "en": "english",
}


def code_to_language(code: str) -> str:
    if not code:
        return "simple"
    normalized = code.lower()
    base = normalized.split("-", 1)[0].split("_", 1)[0]
    return LANGUAGES.get(base, "simple")
