"""Write a report-style description from search keywords with the LLM.

A one-word query embeds to a blurry concept and a whole report to a blurry
mixture. A short passage in the style of a matching report sits much closer to
the reports the user wants, and it is what a user would type into the
description field by hand.
"""

import hashlib
import json
import logging

import openai
from django.conf import settings
from django.core.cache import cache

from radis.core.utils.llm_client import AsyncChatClient, LLMResponseError
from radis.core.utils.rate_limit import RateLimited

logger = logging.getLogger(__name__)

# The extraction query generator's feature: both write search inputs, so one model
# override (LLM_QUERY_GENERATION_MODEL) covers them.
LLM_FEATURE = "query_generation"


def _cache_key(terms: str) -> str:
    """Everything that determines the answer: a shared cache backend outlives
    process restarts and thus model or prompt changes."""
    spec = settings.LLM_MODELS[LLM_FEATURE]
    fingerprint = "\x00".join(
        [
            settings.LLM_BASE_URL,
            spec.model,
            json.dumps(spec.params, sort_keys=True),
            settings.SEARCH_DESCRIPTION_SYSTEM_PROMPT,
            terms,
        ]
    )
    return "search-description-" + hashlib.sha256(fingerprint.encode()).hexdigest()


async def write_description(terms: str) -> str | None:
    """A report-style passage about ``terms``, or None when the LLM could not
    answer in time.

    A user is waiting on a button, so there are no transient retries and the
    rate-limit gate gets the same budget as the request. Answers are cached per
    terms, model and prompt; failures are not, so an outage never pins the terms to
    the failure for the cache lifetime.
    """
    text = " ".join(terms.split())
    if not text:
        return None

    key = _cache_key(text)
    cached = await cache.aget(key)
    if cached is not None:
        return cached

    client: AsyncChatClient | None = None
    try:
        # Inside the try: the OpenAI client refuses to construct without an API key.
        client = AsyncChatClient(LLM_FEATURE, timeout=settings.SEARCH_DESCRIPTION_TIMEOUT)
        answer = await client.chat(
            [
                {"role": "system", "content": settings.SEARCH_DESCRIPTION_SYSTEM_PROMPT},
                {"role": "user", "content": text},
            ],
            max_completion_tokens=settings.SEARCH_DESCRIPTION_MAX_TOKENS,
            max_wait=float(settings.SEARCH_DESCRIPTION_TIMEOUT),
            transient_retry_attempts=0,
        )
    except (openai.OpenAIError, RateLimited, LLMResponseError) as exc:
        logger.warning("The description writer failed: %s", exc)
        return None
    finally:
        if client is not None:
            await client.close()

    description = " ".join(answer.split()).strip('"').strip()
    if not description:
        return None
    await cache.aset(key, description, timeout=settings.SEARCH_DESCRIPTION_CACHE_TIMEOUT_SECONDS)
    return description
