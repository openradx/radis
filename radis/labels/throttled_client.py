from django.conf import settings
from pydantic import BaseModel

from radis.chats.utils.chat_client import ChatClient
from radis.chats.utils.rate_limit import (
    RateLimitGate,
    run_through_gate,
    with_transient_retries,
)

# Process-global so every labeling thread in this worker shares one backoff window.
# Constructor args (base/fallback-max/header-ceiling) are read once at import; per-call
# args (max-wait budget, retry attempts/base) are read per call in extract_data.
_LABELING_GATE = RateLimitGate(
    base_seconds=settings.LABELING_RATE_LIMIT_BACKOFF_BASE_SECONDS,
    fallback_max_seconds=settings.LABELING_RATE_LIMIT_FALLBACK_MAX_SECONDS,
    header_ceiling_seconds=settings.LABELING_RATE_LIMIT_HEADER_CEILING_SECONDS,
)


class ThrottledChatClient:
    """ChatClient wrapper that routes every call through a small transient retry and
    the shared rate-limit gate. Same extract_data surface as ChatClient."""

    def __init__(self, client: ChatClient) -> None:
        self._client = client

    def extract_data(self, prompt: str, schema: type[BaseModel]) -> BaseModel:
        return run_through_gate(
            _LABELING_GATE,
            settings.LABELING_RATE_LIMIT_MAX_WAIT_SECONDS,
            lambda: with_transient_retries(
                lambda: self._client.extract_data(prompt, schema),
                settings.LABELING_TRANSIENT_RETRY_ATTEMPTS,
                settings.LABELING_TRANSIENT_RETRY_BASE_SECONDS,
            ),
        )
