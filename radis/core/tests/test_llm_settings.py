from django.conf import settings as dj_settings


def test_llm_rate_limit_settings_have_expected_defaults():
    assert dj_settings.LLM_REQUEST_TIMEOUT_SECONDS == 60.0
    assert dj_settings.LLM_EXTRA_BODY == {"chat_template_kwargs": {"enable_thinking": False}}
    assert dj_settings.LLM_RATE_LIMIT_BACKOFF_BASE_SECONDS == 5.0
    assert dj_settings.LLM_RATE_LIMIT_FALLBACK_MAX_SECONDS == 120.0
    assert dj_settings.LLM_RATE_LIMIT_HEADER_CEILING_SECONDS == 3600.0
    assert dj_settings.LLM_RATE_LIMIT_MAX_WAIT_SECONDS == 300.0
    assert dj_settings.LLM_RATE_LIMIT_INTERACTIVE_MAX_WAIT_SECONDS == 20.0
    assert dj_settings.LLM_TRANSIENT_RETRY_ATTEMPTS == 2
    assert dj_settings.LLM_TRANSIENT_RETRY_BASE_SECONDS == 1.0
    assert dj_settings.LLM_MAX_RPM == 0
