"""Parsing of the model specs used by the LLM_*_MODEL settings.

A spec names a model and, optionally, the request parameters to send with it:

    qwen3.5:0.8b
    qwen3.5:0.8b?reasoning_effort=none
    qwen3.5:0.8b?reasoning_effort=none&temperature=0
    qwen3:8b?chat_template_kwargs.enable_thinking=false

Parameters are merged into the request body, so both standard OpenAI fields
(``temperature``) and provider extensions (``reasoning_effort``) can be set this way.

This module deliberately imports nothing from Django: the settings module parses specs
while it is still being defined.
"""

import json
from dataclasses import dataclass, field
from urllib.parse import parse_qsl


class ModelSpecError(ValueError):
    """A model spec could not be parsed."""


@dataclass(frozen=True)
class ModelSpec:
    model: str
    params: dict = field(default_factory=dict)


def _parse_value(raw: str):
    """Coerce a query string value to the type it looks like.

    Values are JSON where that succeeds, so ``temperature=0`` sends a number and
    ``enable_thinking=false`` a boolean. Anything else stays a string, which is what
    keeps ``reasoning_effort=none`` the literal "none" that providers expect rather
    than a null.

    ``True``/``False`` are accepted in any casing on purpose: JSON only knows the
    lowercase spelling, and a stray ``enable_thinking=False`` would otherwise become the
    *truthy* string "False" and quietly do the opposite of what it says.
    """
    if raw.lower() in ("true", "false"):
        return raw.lower() == "true"
    try:
        return json.loads(raw)
    except ValueError:
        return raw


def _assign(params: dict, dotted_key: str, value) -> None:
    """Assign a possibly dotted key, expanding it into nested dicts.

    ``chat_template_kwargs.enable_thinking`` becomes
    ``{"chat_template_kwargs": {"enable_thinking": ...}}``, which is how vLLM and
    SGLang expect their template arguments.
    """
    segments = dotted_key.split(".")
    if not all(segment.strip() for segment in segments):
        raise ModelSpecError(f"Parameter name '{dotted_key}' has an empty part")

    *parents, leaf = segments
    target = params
    for parent in parents:
        existing = target.setdefault(parent, {})
        if not isinstance(existing, dict):
            raise ModelSpecError(
                f"Cannot nest '{dotted_key}': '{parent}' is already set to a plain value"
            )
        target = existing
    target[leaf] = value


def parse_model_spec(raw: str) -> ModelSpec:
    """Parse ``model[?param=value&...]``, raising ModelSpecError if it is unusable."""
    model, separator, query = raw.strip().partition("?")
    model = model.strip()
    if not model:
        raise ModelSpecError(f"Model spec {raw!r} does not name a model")

    params: dict = {}
    if separator:
        if not query.strip():
            raise ModelSpecError(f"Model spec {raw!r} ends in '?' but sets no parameters")
        # keep_blank_values so 'x=' reaches us to be rejected below rather than dropped
        for key, value in parse_qsl(query, keep_blank_values=True):
            key = key.strip()
            if not key:
                raise ModelSpecError(f"Model spec {raw!r} has a parameter without a name")
            if not value.strip():
                # An empty value is always a typo, and providers reject it with a 400 on
                # every request. Better to refuse to start than to fail on each call.
                raise ModelSpecError(f"Parameter '{key}' in model spec {raw!r} has no value")
            _assign(params, key, _parse_value(value))

    return ModelSpec(model=model, params=params)
