import json
import random
from functools import cache

from django.conf import settings

LANGUAGES = ("en", "de")


@cache
def _sample_reports(language: str) -> list[str]:
    path = settings.BASE_PATH / "samples" / f"reports_{language}.json"
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def random_sample_report(language: str) -> str:
    """One of the synthetic example reports that ship with RADIS."""
    return random.choice(_sample_reports(language))
