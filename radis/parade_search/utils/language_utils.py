LANGUAGES = {
    "de": "german",
    "en": "english",
}


def code_to_language(code: str) -> str:
    if code in LANGUAGES:
        return LANGUAGES[code]
    else:
        raise ValueError(f"Language {code} is not supported.")
