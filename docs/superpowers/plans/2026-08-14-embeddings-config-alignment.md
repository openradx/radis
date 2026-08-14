# Embeddings Configuration Alignment Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Bring the embedding service configuration on `feat/hybrid-search` into line with the externalized-LLM conventions introduced by #267, so both external providers are configured the same way and hybrid search has exactly one on/off switch.

**Architecture:** Embeddings keep their own configuration family (separate endpoint, separate client, separate rate-limit gate) but inherit the LLM endpoint and key when not overridden — one variable covers the multiplexing providers (OpenAI, Ollama, a gateway), an override covers the split ones (vLLM and SGLang serve one model per process). The embedding *model spec* becomes the feature switch: unset means full-text-only, and it is parsed at startup with the same `model[?param=value&...]` grammar as the LLM models.

**Tech Stack:** Django 6 settings (`environs`), `radis/core/utils/model_spec.py` (`parse_model_spec`, `ModelSpec`), the `openai` SDK, pytest + pytest-django, `override_settings`, `httpx.MockTransport`.

## Global Constraints

- Branch: work on `feat/hybrid-search` (PR #226). Do **not** rebase or force-push it; the branch already contains `main` through `1e16deff`.
- Every settings name that reaches an operator's `.env` uses the **plural** prefix `EMBEDDINGS_`, matching the `embeddings` queue, the `embeddings_worker` service and `EMBEDDINGS_WORKER_CONCURRENCY`. Python module constants (`EMBEDDING_GATE`, `PERMANENT_EMBEDDING_ERRORS`, `EMBEDDING_TASK_RETRY_STRATEGY`) are **not** settings and keep their current names.
- None of these variables have ever been released, so there is no backwards-compatibility shim and no deprecation period. Old names are deleted, not aliased.
- Model spec parameters are merged into the **request body**. Anything that is not a request-body field (the query instruction prefix) stays a separate setting.
- `EMBEDDINGS_DIM` remains the single source of truth for the vector column width, because it is schema-coupled and validated against the migrations by `pgsearch.E001`.
- Tests require the dev containers (Postgres with pgvector): `uv run cli compose-up -- --detach` before running anything. Run tests with `uv run cli test -- <path>`.
- Lint with `uv run cli lint` before each commit; format with `uv run cli format-code`.
- Commit messages follow the repository style already used on this branch (`feat(pgsearch): …`, `refactor(pgsearch): …`, `docs: …`).

## File Structure

| File | Responsibility after this plan |
|---|---|
| `radis/settings/base.py` | Declares the embedding provider settings; inherits URL/key/timeout from the LLM settings; parses `EMBEDDINGS_MODEL` into a `ModelSpec` or `None` at startup |
| `radis/core/tests/test_embeddings_settings.py` | **New.** Tests the two settings helpers (`_inherit_env`, `_resolve_embeddings_model`) the same way `test_llm_settings.py` tests theirs |
| `radis/core/utils/embedding_client.py` | **Moved** from `radis/pgsearch/utils/`. The sync embedding client, reading the new settings and sending the spec's params |
| `radis/core/tests/test_embedding_client.py` | **Moved** from `radis/pgsearch/tests/` alongside its module |
| `radis/pgsearch/providers.py` | Search path: one `EMBEDDINGS_MODEL is None` guard, spec-aware cache fingerprint |
| `radis/pgsearch/tasks.py` | Write path: same guard in `enqueue_embed_reports` |
| `radis/pgsearch/admin.py`, `radis/pgsearch/management/commands/embed_pending.py` | Operator paths: same guard, messages naming `EMBEDDINGS_MODEL` |
| `radis/pgsearch/apps.py` | Adds `pgsearch.E003`: the spec's `dimensions` param must agree with `EMBEDDINGS_DIM` |
| `docs/dev-docs/architecture.md`, `docs/dev-docs/contributing.md`, `example.env` | Operator- and developer-facing documentation of the embedding endpoint |
| `AGENTS.md` (`CLAUDE.md`/`GEMINI.md` symlink to it) | Environment-variable reference and troubleshooting entry for hybrid search |

---

### Task 1: Rename the embedding settings to the plural prefix

Pure rename, no behavior change — done first so every later task can use the final names.
Leaves the module constants alone, and leaves the four provider-facing settings alone
because Task 2 replaces them outright.

**Files:**
- Modify: `radis/settings/base.py`, `radis/pgsearch/tasks.py`, `radis/pgsearch/providers.py`, `radis/pgsearch/models.py`, `radis/pgsearch/apps.py`, `radis/pgsearch/admin.py`, `radis/pgsearch/management/commands/embed_pending.py`, `radis/pgsearch/utils/embedding_client.py`
- Modify: every file under `radis/pgsearch/tests/`
- Modify: `example.env`, `docs/superpowers/specs/hybrid-search.md`

**Interfaces:**
- Produces: `settings.EMBEDDINGS_DIM`, `EMBEDDINGS_QUERY_INSTRUCTION`, `EMBEDDINGS_BATCH_SIZE`, `EMBEDDINGS_SUBJOB_SIZE`, `EMBEDDINGS_LIVE_PRIORITY`, `EMBEDDINGS_BACKFILL_PRIORITY`, `EMBEDDINGS_QUERY_CACHE_TIMEOUT_SECONDS`, `EMBEDDINGS_RATE_LIMIT_*`, `EMBEDDINGS_TRANSIENT_RETRY_*`, `EMBEDDINGS_TASK_*`.
- Consumes: nothing new.

- [ ] **Step 1: Apply the rename**

```bash
cd /Users/kschlamp/workspace/adit-radis-workspace/projects/radis
git grep -l -E "\bEMBEDDING_(DIM|QUERY_INSTRUCTION|BATCH_SIZE|SUBJOB_SIZE|LIVE_PRIORITY|BACKFILL_PRIORITY|QUERY_CACHE_TIMEOUT_SECONDS|RATE_LIMIT_|TRANSIENT_RETRY_|TASK_MAX_ATTEMPTS|TASK_EXPONENTIAL_WAIT_SECONDS)" \
  -- radis example.env docs \
  | xargs sed -i -E 's/\bEMBEDDING_(DIM|QUERY_INSTRUCTION|BATCH_SIZE|SUBJOB_SIZE|LIVE_PRIORITY|BACKFILL_PRIORITY|QUERY_CACHE_TIMEOUT_SECONDS|RATE_LIMIT_|TRANSIENT_RETRY_|TASK_MAX_ATTEMPTS|TASK_EXPONENTIAL_WAIT_SECONDS)/EMBEDDINGS_\1/g'
```

The `\b` prevents matches inside `PERMANENT_EMBEDDING_ERRORS`, and the alternation
excludes `EMBEDDING_GATE` and `EMBEDDING_TASK_RETRY_STRATEGY` — module constants, not
settings, so they keep their names.

- [ ] **Step 2: Verify nothing was missed and nothing extra was hit**

```bash
git grep -n "EMBEDDING_" -- radis example.env docs
```

Expected: only `EMBEDDING_GATE`, `EMBEDDING_TASK_RETRY_STRATEGY` and
`PERMANENT_EMBEDDING_ERRORS` remain. Anything else is a miss — fix it by hand.

- [ ] **Step 3: Run the full pgsearch suite**

Run: `uv run cli test -- radis/pgsearch/ radis/core/ radis/search/`
Expected: PASS — a rename changes no behavior, so a failure here means a missed reference.

- [ ] **Step 4: Lint and commit**

```bash
uv run cli lint
git add -A
git commit -m "refactor(pgsearch): use the plural EMBEDDINGS_ prefix for settings

Matches the embeddings queue, the embeddings_worker service and
EMBEDDINGS_WORKER_CONCURRENCY. None of these variables have shipped, so the old names
are removed rather than aliased."
```

---

### Task 2: Provider settings with LLM inheritance and a model-spec switch

Replaces the four provider-facing settings (`EMBEDDING_PROVIDER_URL`, `EMBEDDING_PROVIDER_API_KEY`, `EMBEDDING_MODEL_NAME`, `EMBEDDING_REQUEST_TIMEOUT`) with inheriting equivalents and a parsed `ModelSpec`, and rewires the client and its one non-client reader (the query cache fingerprint).

**Files:**
- Modify: `radis/settings/base.py` (the `# Embedding service (per-deployment)` block added by this branch, directly after the LLM block that ends with `LLM_RATE_LIMIT_HEADER_CEILING_SECONDS`)
- Modify: `radis/settings/test.py:19-20` (the `EMBEDDING_PROVIDER_URL = ""` override)
- Modify: `radis/pgsearch/utils/embedding_client.py:96-140` (`EmbeddingClient.__init__`, `embed_documents`)
- Modify: `radis/pgsearch/providers.py:236-261` (`_embed_query_cached` fingerprint)
- Create: `radis/core/tests/test_embeddings_settings.py`
- Modify: `radis/pgsearch/tests/test_embedding_client.py` (every `override_settings` block)
- Modify: `radis/pgsearch/tests/test_query_embedding_cache.py`

**Interfaces:**
- Produces: `settings.EMBEDDINGS_BASE_URL: str`, `settings.EMBEDDINGS_API_KEY: str`, `settings.EMBEDDINGS_REQUEST_TIMEOUT_SECONDS: float`, `settings.EMBEDDINGS_MODEL: ModelSpec | None`. `radis.settings.base._inherit_env(name: str, fallback: str) -> str` and `radis.settings.base._resolve_embeddings_model() -> ModelSpec | None` are importable for tests.
- Consumes: `radis.core.utils.model_spec.parse_model_spec`, `ModelSpec`, `ModelSpecError` (already imported at `radis/settings/base.py:19`); `_optional_env` and `LLM_BASE_URL` / `LLM_API_KEY` / `LLM_REQUEST_TIMEOUT_SECONDS` from the LLM block above.

- [ ] **Step 1: Write the failing settings tests**

Create `radis/core/tests/test_embeddings_settings.py`:

```python
import pytest
from django.conf import settings as dj_settings
from django.core.exceptions import ImproperlyConfigured

from radis.settings.base import _inherit_env, _resolve_embeddings_model


@pytest.fixture
def clean_embeddings_env(monkeypatch):
    """Start from no embedding configuration at all."""
    for name in ("EMBEDDINGS_MODEL", "EMBEDDINGS_BASE_URL", "EMBEDDINGS_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    return monkeypatch


def test_an_unset_setting_inherits_its_llm_counterpart(clean_embeddings_env):
    assert _inherit_env("EMBEDDINGS_BASE_URL", "https://llm.example/v1") == "https://llm.example/v1"


@pytest.mark.parametrize("raw", ["", "   "])
def test_a_blank_setting_counts_as_unset(clean_embeddings_env, raw: str):
    # A compose file passes every variable through, empty when it is not configured.
    clean_embeddings_env.setenv("EMBEDDINGS_BASE_URL", raw)

    assert _inherit_env("EMBEDDINGS_BASE_URL", "https://llm.example/v1") == "https://llm.example/v1"


def test_a_configured_setting_overrides_the_llm_counterpart(clean_embeddings_env):
    # The split case: a self-hosted vLLM serves one model per process, so the
    # embedding endpoint is a different server than the chat endpoint.
    clean_embeddings_env.setenv("EMBEDDINGS_BASE_URL", "https://embed.example/v1")

    assert _inherit_env("EMBEDDINGS_BASE_URL", "https://llm.example/v1") == "https://embed.example/v1"


def test_no_model_configured_means_hybrid_search_is_off(clean_embeddings_env):
    assert _resolve_embeddings_model() is None


def test_a_blank_model_counts_as_unset(clean_embeddings_env):
    clean_embeddings_env.setenv("EMBEDDINGS_MODEL", "   ")

    assert _resolve_embeddings_model() is None


def test_the_model_spec_carries_request_parameters(clean_embeddings_env):
    clean_embeddings_env.setenv("EMBEDDINGS_MODEL", "text-embedding-3-large?dimensions=1024")

    spec = _resolve_embeddings_model()

    assert spec is not None
    assert spec.model == "text-embedding-3-large"
    assert spec.params == {"dimensions": 1024}


def test_a_malformed_model_names_the_setting_at_fault(clean_embeddings_env):
    # A bare ModelSpecError would leave the admin guessing which variable is wrong.
    clean_embeddings_env.setenv("EMBEDDINGS_MODEL", "?dimensions=1024")

    with pytest.raises(ImproperlyConfigured, match="EMBEDDINGS_MODEL"):
        _resolve_embeddings_model()


def test_the_base_url_defaults_to_the_llm_endpoint():
    # Only the fallback is under test; a value configured for this environment is a
    # deployment choice, not a regression.
    import os

    if os.environ.get("EMBEDDINGS_BASE_URL", "").strip():
        pytest.skip("EMBEDDINGS_BASE_URL is set in the environment, so its default is not in play")

    assert dj_settings.EMBEDDINGS_BASE_URL == dj_settings.LLM_BASE_URL
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run cli test -- radis/core/tests/test_embeddings_settings.py`
Expected: FAIL — `ImportError: cannot import name '_inherit_env' from 'radis.settings.base'`

- [ ] **Step 3: Replace the four provider settings in `radis/settings/base.py`**

Delete these four lines from the embedding block:

```python
EMBEDDING_PROVIDER_URL = env.str("EMBEDDING_PROVIDER_URL", default="")
EMBEDDING_PROVIDER_API_KEY = env.str("EMBEDDING_PROVIDER_API_KEY", default="")
EMBEDDING_MODEL_NAME = env.str("EMBEDDING_MODEL_NAME", default="Qwen/Qwen3-Embedding-4B")
EMBEDDING_REQUEST_TIMEOUT = env.int("EMBEDDING_REQUEST_TIMEOUT", default=30)
```

and put this at the top of the block in their place (leave `EMBEDDING_DIM` and the tuning
constants below it untouched — Task 1 already renamed those to `EMBEDDINGS_DIM` and
`EMBEDDINGS_QUERY_INSTRUCTION`):

```python
# Embedding service (per-deployment)
#
# Embeddings are inference too, but a different model class. One endpoint serves both
# only when the provider multiplexes models (OpenAI, Ollama, an LLM gateway); a
# self-hosted vLLM or SGLang serves one model per process, so there the embedding
# endpoint is a second URL. Inherit the LLM endpoint and override only where they
# actually diverge, so the common deployment configures one URL rather than two.
def _inherit_env(name: str, fallback: str) -> str:
    """An embedding setting that falls back to its LLM counterpart when unset.

    A blank is what an `.env` line like `EMBEDDINGS_BASE_URL=` produces, and it means
    the same as leaving the line out.
    """
    return env.str(name, default="").strip() or fallback


EMBEDDINGS_BASE_URL = _inherit_env("EMBEDDINGS_BASE_URL", LLM_BASE_URL)
EMBEDDINGS_API_KEY = _inherit_env("EMBEDDINGS_API_KEY", LLM_API_KEY)
EMBEDDINGS_REQUEST_TIMEOUT_SECONDS = _optional_env(
    "EMBEDDINGS_REQUEST_TIMEOUT_SECONDS", float, LLM_REQUEST_TIMEOUT_SECONDS
)


def _resolve_embeddings_model() -> ModelSpec | None:
    """The embedding model, or None when hybrid search is not configured.

    Unlike the LLM models this one is optional: without it RADIS serves full-text
    search only, which is a complete product rather than a broken one. That makes the
    model the feature switch — every embedding code path asks this one question
    instead of inferring configuredness from a URL that now has a fallback anyway.

    Same 'model[?param=value&...]' grammar as the LLM models, so request-body
    parameters (OpenAI's `dimensions`, a provider's `truncate`) are configured
    alongside the model. Parsed here so a malformed spec is a boot error naming the
    setting at fault, not a 400 on the first search.
    """
    raw = env.str("EMBEDDINGS_MODEL", default="").strip()
    if not raw:
        return None
    try:
        return parse_model_spec(raw)
    except ModelSpecError as err:
        raise ImproperlyConfigured(f"Invalid EMBEDDINGS_MODEL: {err}") from err


EMBEDDINGS_MODEL = _resolve_embeddings_model()
```

- [ ] **Step 4: Run the settings tests to verify they pass**

Run: `uv run cli test -- radis/core/tests/test_embeddings_settings.py`
Expected: PASS (9 tests)

- [ ] **Step 5: Point the test settings at the new switch**

In `radis/settings/test.py`, replace the `EMBEDDING_PROVIDER_URL = ""` block with:

```python
# Tests must not hit a live embedding service. Embedding work is deferred via a
# Procrastinate task and tests do not run a worker by default, but leaving the model
# unset also means any incidental EmbeddingClient construction fast-fails into
# EmbeddingClientError rather than touching the network. Tests that exercise the
# embedding path set EMBEDDINGS_MODEL explicitly and patch the client.
EMBEDDINGS_MODEL = None
```

- [ ] **Step 6: Write the failing client test for spec parameters**

In `radis/pgsearch/tests/test_embedding_client.py`, add this test at the end of the file
(it fails now because the client sends no request parameters):

```python
@override_settings(
    EMBEDDINGS_BASE_URL="http://embed.example/v1",
    EMBEDDINGS_API_KEY="",
    EMBEDDINGS_MODEL=parse_model_spec("text-embedding-3-large?dimensions=2"),
    EMBEDDINGS_DIM=2,
    EMBEDDINGS_REQUEST_TIMEOUT_SECONDS=10.0,
    EMBEDDINGS_QUERY_INSTRUCTION="",
)
def test_model_spec_parameters_reach_the_request_body(monkeypatch):
    from radis.core.utils import embedding_client as ec

    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "object": "list",
                "data": [{"object": "embedding", "index": 0, "embedding": [1.0, 0.0]}],
                "model": "text-embedding-3-large",
                "usage": {"prompt_tokens": 1, "total_tokens": 1},
            },
        )

    _install_transport(monkeypatch, handler)
    ec.EmbeddingClient().embed_documents(["hello"])

    assert seen["body"]["model"] == "text-embedding-3-large"
    # `dimensions` is a real OpenAI request field: asking the provider for the width we
    # store beats truncating a larger vector client-side.
    assert seen["body"]["dimensions"] == 2
```

Add `from radis.core.utils.model_spec import parse_model_spec` to the file's imports.

Note: the module path in the import is `radis.core.utils.embedding_client`, where Task 5
moves it. Until then this one test imports from `radis.pgsearch.utils.embedding_client` —
write it against the current path, and Task 5's sed will move it with the rest.

- [ ] **Step 7: Update every `override_settings` block in the client tests**

Mechanical, in `radis/pgsearch/tests/test_embedding_client.py` — the helper at the top and
each decorator further down. Old keys → new keys:

| Old | New |
|---|---|
| `EMBEDDING_PROVIDER_URL="http://embed.example/v1"` | `EMBEDDINGS_BASE_URL="http://embed.example/v1"` |
| `EMBEDDING_PROVIDER_API_KEY="secret"` | `EMBEDDINGS_API_KEY="secret"` |
| `EMBEDDING_MODEL_NAME="qwen3"` | `EMBEDDINGS_MODEL=parse_model_spec("qwen3")` |
| `EMBEDDING_REQUEST_TIMEOUT=10` | `EMBEDDINGS_REQUEST_TIMEOUT_SECONDS=10.0` |
| `EMBEDDING_DIM=4` | `EMBEDDINGS_DIM=4` |
| `EMBEDDING_QUERY_INSTRUCTION="INST: "` | `EMBEDDINGS_QUERY_INSTRUCTION="INST: "` |

Add one test for the unconfigured case:

```python
@override_settings(EMBEDDINGS_MODEL=None)
def test_construction_fails_fast_when_no_model_is_configured():
    from radis.pgsearch.utils import embedding_client as ec

    with pytest.raises(ec.EmbeddingClientError, match="EMBEDDINGS_MODEL"):
        ec.EmbeddingClient()
```

- [ ] **Step 8: Run the client tests to verify they fail**

Run: `uv run cli test -- radis/pgsearch/tests/test_embedding_client.py`
Expected: FAIL — `AttributeError: 'Settings' object has no attribute 'EMBEDDING_PROVIDER_URL'` from `EmbeddingClient.__init__`, and `KeyError: 'dimensions'` in the new parameter test.

- [ ] **Step 9: Rewire `EmbeddingClient` to the new settings**

In `radis/pgsearch/utils/embedding_client.py`, replace `__init__` and the `embeddings.create` call:

```python
    def __init__(self) -> None:
        spec = settings.EMBEDDINGS_MODEL
        if spec is None:
            raise EmbeddingClientError(
                "EMBEDDINGS_MODEL is not configured; hybrid search is disabled"
            )
        # SDK rejects an empty api_key at construction; "unused" is the documented
        # placeholder for self-hosted endpoints that ignore auth (Ollama, vLLM).
        api_key = settings.EMBEDDINGS_API_KEY or "unused"
        self._http = _build_http_client()
        self._client = openai.OpenAI(
            base_url=settings.EMBEDDINGS_BASE_URL,
            api_key=api_key,
            http_client=self._http,
            max_retries=0,  # 429s are handled by the rate-limit gate, not the SDK
            timeout=settings.EMBEDDINGS_REQUEST_TIMEOUT_SECONDS,
        )
        self._model = spec.model
        # Request parameters configured with the model, e.g. OpenAI's `dimensions`.
        self._extra_body = spec.params
        self._dim = settings.EMBEDDINGS_DIM
        self._instruction = settings.EMBEDDINGS_QUERY_INSTRUCTION
```

```python
        response = self._client.embeddings.create(
            model=self._model,
            input=texts,
            encoding_format="float",
            extra_body=self._extra_body,
        )
```

Update the class docstring's mention of `EMBEDDING_PROVIDER_URL` to `EMBEDDINGS_BASE_URL`.

- [ ] **Step 10: Run the client tests to verify they pass**

Run: `uv run cli test -- radis/pgsearch/tests/test_embedding_client.py`
Expected: PASS

- [ ] **Step 11: Make the query cache fingerprint spec-aware**

The fingerprint must cover everything that determines the vector. `dimensions=512` and
`dimensions=1024` on the same model produce different vectors, so the params belong in
the key. In `radis/pgsearch/providers.py`, replace the fingerprint in `_embed_query_cached`:

```python
    spec = settings.EMBEDDINGS_MODEL
    if spec is None:
        # Nothing to embed against. Task 3 adds the caller-side guard so this helper is
        # not even reached in an FTS-only deployment; returning None keeps it correct on
        # its own in the meantime, and afterwards for any future caller.
        return None
    fingerprint = "\x00".join(
        [
            spec.model,
            json.dumps(spec.params, sort_keys=True),
            settings.EMBEDDINGS_QUERY_INSTRUCTION,
            str(settings.EMBEDDINGS_DIM),
            query_text,
        ]
    )
```

Add `import json` to the module's imports.

- [ ] **Step 12: Update the cache test and run the affected suites**

In `radis/pgsearch/tests/test_query_embedding_cache.py`, change any `EMBEDDING_MODEL_NAME=`
override to `EMBEDDINGS_MODEL=parse_model_spec(...)` and add a case proving params
participate in the key:

```python
def test_differing_spec_parameters_do_not_share_a_cache_entry(monkeypatch):
    # dimensions=2 and dimensions=4 are different vectors for the same model text.
    calls = []

    def fake_embed(text, caller):
        calls.append(text)
        return [1.0, 0.0]

    monkeypatch.setattr(providers, "_embed_query_or_none", fake_embed)

    with override_settings(EMBEDDINGS_MODEL=parse_model_spec("qwen3?dimensions=2")):
        providers._embed_query_cached("pneumonia", "test")
    with override_settings(EMBEDDINGS_MODEL=parse_model_spec("qwen3?dimensions=4")):
        providers._embed_query_cached("pneumonia", "test")

    assert len(calls) == 2
```

Run: `uv run cli test -- radis/pgsearch/ radis/core/`
Expected: PASS

- [ ] **Step 13: Lint and commit**

```bash
uv run cli format-code
uv run cli lint
git add radis/settings/base.py radis/settings/test.py radis/core/tests/test_embeddings_settings.py \
        radis/pgsearch/utils/embedding_client.py radis/pgsearch/providers.py \
        radis/pgsearch/tests/test_embedding_client.py radis/pgsearch/tests/test_query_embedding_cache.py
git commit -m "refactor(pgsearch): configure embeddings like the LLM endpoint

EMBEDDINGS_BASE_URL and EMBEDDINGS_API_KEY inherit their LLM counterparts, so the
providers that serve chat and embeddings from one endpoint are configured once, and
the ones that need two servers still can be. EMBEDDINGS_MODEL takes the same
'model[?param=value&...]' spec as the LLM models and is parsed at startup, which puts
OpenAI's 'dimensions' where the model is configured rather than only in the
client-side truncation."
```

---

### Task 3: One enablement guard for every embedding code path

Today four code paths ask "is embedding configured?" and three of them answer it with
`if not settings.EMBEDDING_PROVIDER_URL`. The fourth — the search path — never asks, so an
unconfigured deployment logs a full traceback per query via `logger.exception`. After
Task 2 the URL always has a value (it inherits `LLM_BASE_URL`), so that check is not just
duplicated but wrong. All four now ask `settings.EMBEDDINGS_MODEL is None`.

**Files:**
- Modify: `radis/pgsearch/providers.py:286-291` (the `search()`/`retrieve()` embedding call)
- Modify: `radis/pgsearch/tasks.py:164-172` (`enqueue_embed_reports`)
- Modify: `radis/pgsearch/admin.py:177-184`
- Modify: `radis/pgsearch/management/commands/embed_pending.py:71-76`
- Modify: `radis/pgsearch/tests/test_provider_hybrid.py`, `test_embed_reports_task.py`, `test_admin.py`, `test_embed_pending_command.py`

**Interfaces:**
- Consumes: `settings.EMBEDDINGS_MODEL` from Task 2.
- Produces: no new symbols; the invariant that `_embed_query_cached` is not reached at all when `EMBEDDINGS_MODEL is None` (its own early return from Task 2, Step 11 stays as defence in depth).

- [ ] **Step 1: Write the failing search-path test**

Add to `radis/pgsearch/tests/test_provider_hybrid.py`:

```python
@override_settings(EMBEDDINGS_MODEL=None)
def test_search_without_a_configured_model_makes_no_embedding_call(
    group, reports_with_embeddings, caplog, monkeypatch
):
    """An FTS-only deployment must not pay for, or log, a failed embedding attempt.

    Constructing the client raises when no model is configured, and the search path
    catches that into logger.exception — a full traceback on every single query.
    """
    from radis.pgsearch import providers

    calls = []
    monkeypatch.setattr(
        providers, "_embed_query_cached", lambda text, caller: calls.append(text)
    )

    with caplog.at_level(logging.ERROR, logger="radis.pgsearch.providers"):
        result = search(_make_search("pneumothorax", group.pk))

    assert calls == []
    assert caplog.records == []
    # FTS still works: r0 and r2 both mention pneumothorax.
    assert result.total_count == 2
```

`_make_search(query_str, group_id)`, the `group` fixture and the
`reports_with_embeddings` fixture already exist at the top of this file; `search` is
already imported from `radis.pgsearch.providers`. Add `import logging` and
`from django.test import override_settings` to the imports.

- [ ] **Step 2: Run it to verify it fails**

Run: `uv run cli test -- radis/pgsearch/tests/test_provider_hybrid.py -k no_embedding_call`
Expected: FAIL — `assert caplog.records == []` fails with one ERROR record whose message
ends in "embedding service looks misconfigured".

- [ ] **Step 3: Guard the search path**

In `radis/pgsearch/providers.py`:

```python
    # Vector side: skipped entirely when no embedding model is configured (FTS-only
    # deployment), and when stripping NOT branches leaves nothing to embed (see
    # docs/superpowers/specs/hybrid-search.md §7.8).
    query_text = QueryParser.unparse_for_embedding(search.query)
    query_vec: list[float] | None = None
    if settings.EMBEDDINGS_MODEL is not None and query_text.strip():
        query_vec = _embed_query_cached(query_text, caller)
```

- [ ] **Step 4: Run it to verify it passes**

Run: `uv run cli test -- radis/pgsearch/tests/test_provider_hybrid.py -k no_embedding_call`
Expected: PASS

- [ ] **Step 5: Switch the other three guards**

`radis/pgsearch/tasks.py`, in `enqueue_embed_reports`:

```python
    if settings.EMBEDDINGS_MODEL is None:
        # FTS-only/unconfigured deployment: enqueuing embedding subjobs here would
        # only create Procrastinate jobs doomed to fail at client construction.
        # Skip them; search already runs FTS-only.
        logger.info(
            "enqueue_embed_reports: EMBEDDINGS_MODEL not configured; "
            "skipping embedding of %d report(s) (FTS-only deployment)",
            len(report_ids),
        )
        return 0
```

`radis/pgsearch/admin.py`:

```python
        if settings.EMBEDDINGS_MODEL is None:
            self.message_user(
                request,
                "EMBEDDINGS_MODEL is not configured; cannot enqueue embeddings. "
                "Configure the embedding model first.",
                level=messages.WARNING,
            )
            return
```

`radis/pgsearch/management/commands/embed_pending.py`:

```python
        if settings.EMBEDDINGS_MODEL is None:
            raise CommandError(
                "EMBEDDINGS_MODEL is not configured; cannot backfill embeddings. "
                "Configure the embedding model first, or leave it unset to run "
                "FTS-only (reports are still fully searchable via full-text search)."
            )
```

- [ ] **Step 6: Update the tests that set the old switch**

In `test_admin.py`, `test_embed_pending_command.py` and `test_embed_reports_task.py`,
replace every `settings.EMBEDDING_PROVIDER_URL = "http://embedder.local/v1"` with
`settings.EMBEDDINGS_MODEL = parse_model_spec("qwen3")` and every
`settings.EMBEDDING_PROVIDER_URL = ""` with `settings.EMBEDDINGS_MODEL = None`. Update the
`pytest.raises(CommandError, match="EMBEDDING_PROVIDER_URL")` in
`test_embed_pending_command.py:29` to `match="EMBEDDINGS_MODEL"`.

Each of those three files needs `from radis.core.utils.model_spec import parse_model_spec`
added to its imports — `override_settings(EMBEDDINGS_MODEL="qwen3")` would pass a `str`
where the code expects a `ModelSpec` and fail with `AttributeError: 'str' object has no
attribute 'model'`.

- [ ] **Step 7: Run the full suite**

Run: `uv run cli test -- radis/pgsearch/ radis/search/`
Expected: PASS

- [ ] **Step 8: Lint and commit**

```bash
uv run cli format-code
uv run cli lint
git add radis/pgsearch/
git commit -m "fix(pgsearch): ask one question about whether embedding is configured

The search path never checked, so a deployment without an embedding service logged a
full traceback for every query while quietly serving FTS-only results. The other three
paths checked EMBEDDING_PROVIDER_URL, which no longer answers the question now that the
URL falls back to LLM_BASE_URL. All four now test EMBEDDINGS_MODEL."
```

---

### Task 4: Reject a `dimensions` parameter that disagrees with the schema

`EMBEDDINGS_DIM` is schema-coupled and already checked against the migrations
(`pgsearch.E001`). Now that a spec can also carry `?dimensions=N`, two settings can
describe the column width — and disagree. `dimensions=512` against a `vector(1024)`
column makes the provider return short vectors that pgvector rejects on write.

**Files:**
- Modify: `radis/pgsearch/apps.py` (after `check_embedding_dim_matches_migration`)
- Modify: `radis/pgsearch/tests/test_apps_checks.py`

**Interfaces:**
- Consumes: `settings.EMBEDDINGS_MODEL`, `settings.EMBEDDINGS_DIM`.
- Produces: `radis.pgsearch.apps.check_embeddings_dimensions_param(app_configs, **kwargs) -> list[Error]`, error id `pgsearch.E003`.

- [ ] **Step 1: Write the failing check tests**

Add to `radis/pgsearch/tests/test_apps_checks.py`:

```python
from radis.core.utils.model_spec import parse_model_spec
from radis.pgsearch.apps import check_embeddings_dimensions_param


@override_settings(EMBEDDINGS_MODEL=None, EMBEDDINGS_DIM=1024)
def test_no_model_configured_is_not_a_dimensions_error():
    assert check_embeddings_dimensions_param(None) == []


@override_settings(EMBEDDINGS_MODEL=parse_model_spec("qwen3"), EMBEDDINGS_DIM=1024)
def test_a_spec_without_dimensions_is_not_an_error():
    # Client-side Matryoshka truncation covers providers that ignore the field.
    assert check_embeddings_dimensions_param(None) == []


@override_settings(EMBEDDINGS_MODEL=parse_model_spec("qwen3?dimensions=1024"), EMBEDDINGS_DIM=1024)
def test_agreeing_dimensions_are_not_an_error():
    assert check_embeddings_dimensions_param(None) == []


@override_settings(EMBEDDINGS_MODEL=parse_model_spec("qwen3?dimensions=512"), EMBEDDINGS_DIM=1024)
def test_disagreeing_dimensions_are_reported():
    errors = check_embeddings_dimensions_param(None)

    assert [error.id for error in errors] == ["pgsearch.E003"]
    assert "512" in errors[0].msg and "1024" in errors[0].msg
```

- [ ] **Step 2: Run to verify they fail**

Run: `uv run cli test -- radis/pgsearch/tests/test_apps_checks.py -k dimensions`
Expected: FAIL — `ImportError: cannot import name 'check_embeddings_dimensions_param'`

- [ ] **Step 3: Add the check**

In `radis/pgsearch/apps.py`, after the existing check:

```python
@register()
def check_embeddings_dimensions_param(app_configs, **kwargs):
    """Fail loudly when the model spec's `dimensions` disagrees with EMBEDDINGS_DIM.

    Both describe the width of the stored vector: `dimensions` asks the provider for
    it, EMBEDDINGS_DIM is what the column was migrated to. Disagreement surfaces as an
    opaque pgvector dimension error on the first write, long after the deploy.
    """
    spec = settings.EMBEDDINGS_MODEL
    if spec is None:
        return []

    requested = spec.params.get("dimensions")
    if requested is None or requested == settings.EMBEDDINGS_DIM:
        return []

    return [
        Error(
            f"EMBEDDINGS_MODEL requests dimensions={requested} but EMBEDDINGS_DIM is "
            f"{settings.EMBEDDINGS_DIM}. The provider would return vectors the "
            f"embedding column cannot store.",
            id="pgsearch.E003",
            hint=(
                "Drop the 'dimensions' parameter to let the client truncate to "
                "EMBEDDINGS_DIM, or set the two to the same value."
            ),
        )
    ]
```

- [ ] **Step 4: Run to verify they pass**

Run: `uv run cli test -- radis/pgsearch/tests/test_apps_checks.py`
Expected: PASS

- [ ] **Step 5: Verify the check fires end to end**

```bash
EMBEDDINGS_MODEL='qwen3?dimensions=512' uv run python manage.py check
```

Expected: exits non-zero with `pgsearch.E003`.

- [ ] **Step 6: Commit**

```bash
uv run cli lint
git add radis/pgsearch/apps.py radis/pgsearch/tests/test_apps_checks.py
git commit -m "feat(pgsearch): reject a dimensions parameter the column cannot store"
```

---

### Task 5: Move the embedding client next to the LLM client

`radis/core/utils/` is where #267 put external-provider plumbing, and the rate-limit gate
this module depends on already lives there. The reranker sketched in §11.2 of the spec
would need the same client from outside pgsearch.

**Files:**
- Move: `radis/pgsearch/utils/embedding_client.py` → `radis/core/utils/embedding_client.py`
- Move: `radis/pgsearch/tests/test_embedding_client.py` → `radis/core/tests/test_embedding_client.py`
- Modify: `radis/pgsearch/providers.py`, `radis/pgsearch/tasks.py` (imports)

**Interfaces:**
- Produces: `radis.core.utils.embedding_client` exporting `EmbeddingClient`, `EmbeddingClientError`, `EMBEDDING_GATE`, `PERMANENT_EMBEDDING_ERRORS` — same names, new module path.

- [ ] **Step 1: Move both files with git**

```bash
git mv radis/pgsearch/utils/embedding_client.py radis/core/utils/embedding_client.py
git mv radis/pgsearch/tests/test_embedding_client.py radis/core/tests/test_embedding_client.py
```

- [ ] **Step 2: Update every import**

```bash
git grep -l "pgsearch.utils.embedding_client\|pgsearch\.utils import embedding_client" -- radis \
  | xargs sed -i 's/radis\.pgsearch\.utils\.embedding_client/radis.core.utils.embedding_client/g; s/from radis\.pgsearch\.utils import embedding_client/from radis.core.utils import embedding_client/g'
```

Then fix the relative imports by hand — `radis/pgsearch/providers.py` and
`radis/pgsearch/tasks.py` use `from .utils.embedding_client import (...)`, which must
become `from radis.core.utils.embedding_client import (...)`.

- [ ] **Step 3: Verify no reference to the old path survives**

```bash
git grep -n "pgsearch.utils.embedding_client\|pgsearch/utils/embedding_client" -- radis docs
```

Expected: no hits in `radis/`. Hits in `docs/superpowers/specs/hybrid-search.md` are
handled in Task 6.

- [ ] **Step 4: Run the affected suites**

Run: `uv run cli test -- radis/core/ radis/pgsearch/`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
uv run cli lint
git add -A
git commit -m "refactor(core): move the embedding client next to the LLM client

Both are external-provider plumbing over the openai SDK, and both sit on the rate-limit
gate that already lives in radis/core/utils."
```

---

### Task 6: Document the embedding endpoint where the LLM endpoint is documented

#267 made `docs/dev-docs/architecture.md` and `docs/dev-docs/contributing.md` the place
where "RADIS ships no inference server, point it here" is explained. A developer setting
up the branch today finds nothing about the second external service.

**Files:**
- Modify: `docs/dev-docs/architecture.md` (after the **Structured Output** paragraph that closes the "LLM Configuration" section)
- Modify: `docs/dev-docs/contributing.md` (after the "LLM Setup" section)
- Modify: `example.env` (the embedding block added by this branch)
- Modify: `docs/superpowers/specs/hybrid-search.md` §4.4, §5.3, §12 and the config table in §9
- Update the PR #226 description on GitHub

- [ ] **Step 1: Rewrite the `example.env` block**

```env
# Embedding service (OpenAI-compatible /v1/embeddings), used by hybrid search.
#
# Leave EMBEDDINGS_MODEL empty to run full-text search only — reports stay fully
# searchable, no embedding jobs are queued and the service is never called.
#
# The endpoint and key default to LLM_BASE_URL and LLM_API_KEY, which is what you want
# when one provider serves both (OpenAI, Ollama, an LLM gateway). Set them only when
# embeddings live somewhere else — a self-hosted vLLM or SGLang serves one model per
# process, so there the embedding model is a second server.
#EMBEDDINGS_BASE_URL=
#EMBEDDINGS_API_KEY=

# The model, as 'model[?param=value&...]' — the same spec the LLM_*_MODEL settings take.
# Parameters are merged into the request body, so a provider that supports OpenAI's
# 'dimensions' can be asked for the width directly:
#   EMBEDDINGS_MODEL=Qwen/Qwen3-Embedding-4B
#   EMBEDDINGS_MODEL=text-embedding-3-large?dimensions=1024
# For Ollama in dev: ollama pull dengcao/Qwen3-Embedding-4B:Q5_K_M
EMBEDDINGS_MODEL=

# Vector dimension. Schema-coupled: changing this after deploy requires dropping the
# embedding column, re-migrating, and running `./manage.py embed_pending`. When the
# model spec also sets 'dimensions', the two must agree (checked at startup).
EMBEDDINGS_DIM=1024

# Instruction prefix prepended to search queries before embedding. Model-specific:
# Qwen3-Embedding wants one, text-embedding-3 wants none. Not a request parameter, so
# it is not part of the model spec.
#EMBEDDINGS_QUERY_INSTRUCTION=

# Throughput tuning (all optional).
#EMBEDDINGS_REQUEST_TIMEOUT_SECONDS=60
#EMBEDDINGS_BATCH_SIZE=200
#EMBEDDINGS_SUBJOB_SIZE=1000
#EMBEDDINGS_WORKER_CONCURRENCY=2
```

- [ ] **Step 2: Add the architecture.md section**

Insert after the **Structured Output** paragraph, still inside "LLM Configuration":

```markdown
**Embeddings**: Hybrid search adds a second external service, an OpenAI-compatible
`/v1/embeddings` endpoint. `EMBEDDINGS_MODEL` both names the model and switches the
feature on — left unset, RADIS runs full-text search only, queues no embedding work and
never calls the service. It takes the same `model[?param=value&...]` spec as the LLM
models, so a provider supporting OpenAI's `dimensions` is asked for the stored width
directly instead of the client truncating a larger vector. `EMBEDDINGS_BASE_URL` and
`EMBEDDINGS_API_KEY` default to `LLM_BASE_URL` and `LLM_API_KEY`: one endpoint serves
both when the provider multiplexes models (OpenAI, Ollama, a gateway), while a
self-hosted vLLM or SGLang serves one model per process and needs the override. The
service has its own rate-limit gate — a 429 from the embedding gateway must not pause
inference — and its own worker (`radis-embeddings_worker-1`) draining the `embeddings`
queue, so a million-report backfill cannot starve extractions.
```

Also extend the container list further up with:

```markdown
**Embeddings Worker Container (`radis-embeddings_worker-1`)**: Drains the embeddings
queue — generating and storing report vectors for hybrid search, including operator
backfills started by `./manage.py embed_pending` or the admin action.
```

- [ ] **Step 3: Add the contributing.md section**

Insert directly after the "LLM Setup" section (before "#### Ollama on your machine",
which then serves both):

```markdown
### Embedding Setup (optional)

Hybrid search needs a second model, an embedding model, reached over the same kind of
OpenAI-compatible endpoint. It is optional: with `EMBEDDINGS_MODEL` empty, search runs
full-text only and nothing else changes — no queued jobs, no failed calls.

To turn it on with the Ollama you already have:

```terminal
ollama pull dengcao/Qwen3-Embedding-4B:Q5_K_M
```

```env
EMBEDDINGS_MODEL=dengcao/Qwen3-Embedding-4B:Q5_K_M
```

`EMBEDDINGS_BASE_URL` and `EMBEDDINGS_API_KEY` are not needed here — they fall back to
`LLM_BASE_URL` and `LLM_API_KEY`, and Ollama serves both models from one endpoint. Set
them only if your embedding model lives on a different server, which is the normal case
for vLLM and SGLang since they serve one model per process.

Check that the endpoint actually serves the model, the same way you would check the LLM one:

```terminal
docker compose exec web sh -c 'curl -sf -H "Authorization: Bearer ${EMBEDDINGS_API_KEY:-$LLM_API_KEY}" "${EMBEDDINGS_BASE_URL:-$LLM_BASE_URL}/models"'
```

The `:-` fallbacks mirror what the settings do, so this probes the endpoint the app will
actually use whether or not you overrode it.

Existing reports are not embedded retroactively by the switch. Backfill them with:

```terminal
docker compose exec web ./manage.py embed_pending
```

A GGUF-quantized embedding model produces slightly different vectors than the bf16
reference, so dev embeddings are not interchangeable with production ones — after
swapping models, clear the column and run `embed_pending` again.
```

- [ ] **Step 4: Add the variables to `AGENTS.md`**

`AGENTS.md` is the real file; `CLAUDE.md` and `GEMINI.md` are symlinks to it, so editing it
once covers all three. Its "Environment Variables" section lists every `LLM_*` variable and
has no embedding entry at all. Add after the `LLM_*` list:

```markdown
Hybrid search embeddings (`radis.pgsearch`):

- `EMBEDDINGS_MODEL`: Embedding model, same `model[?param=value&...]` form as the LLM
  models. Empty means full-text search only — no embedding jobs, no calls to the service
- `EMBEDDINGS_BASE_URL`, `EMBEDDINGS_API_KEY`: Default to `LLM_BASE_URL` / `LLM_API_KEY`.
  Set only when embeddings are served from a different endpoint, which is the normal case
  for vLLM and SGLang since they serve one model per process
- `EMBEDDINGS_DIM`: Vector width (default `1024`). Schema-coupled — it must match the
  pgsearch migrations and any `dimensions` parameter in `EMBEDDINGS_MODEL`, both checked
  at startup (`pgsearch.E001`, `pgsearch.E003`)
- `EMBEDDINGS_QUERY_INSTRUCTION`: Instruction prefix prepended to search queries.
  Model-specific; not a request parameter, so it is not part of the model spec
- `EMBEDDINGS_BATCH_SIZE`, `EMBEDDINGS_SUBJOB_SIZE`, `EMBEDDINGS_WORKER_CONCURRENCY`,
  `EMBEDDINGS_REQUEST_TIMEOUT_SECONDS`: Throughput tuning
```

Add a matching Troubleshooting entry, mirroring "LLM Operations Failing":

```markdown
### Hybrid Search Returns Only Full-Text Results

- Confirm `EMBEDDINGS_MODEL` is set — empty is the documented way to run FTS-only
- Check `docker compose logs embeddings_worker` for failed subjobs
- Reports ingested before the model was configured have no vector; run
  `docker compose exec web ./manage.py embed_pending` to backfill them
- A search logs a WARNING and degrades to FTS-only when the embedding service is rate
  limiting or unreachable; the log line names which
- Verify the endpoint serves the model: `docker compose exec web sh -c 'curl -sf -H
  "Authorization: Bearer ${EMBEDDINGS_API_KEY:-$LLM_API_KEY}"
  "${EMBEDDINGS_BASE_URL:-$LLM_BASE_URL}/models"'`
```

Update the "Docker Services" list in the same file with the `embeddings_worker` container
(Procrastinate queue: `embeddings`).

- [ ] **Step 5: Update the spec to match the implementation**

In `docs/superpowers/specs/hybrid-search.md`, update every occurrence of the old names
(`EMBEDDING_PROVIDER_URL`, `EMBEDDING_PROVIDER_API_KEY`, `EMBEDDING_MODEL_NAME`,
`EMBEDDING_REQUEST_TIMEOUT`, and the `radis/pgsearch/utils/embedding_client.py` path),
and add a paragraph to §5.3 recording the decision:

```markdown
Configuration follows the LLM settings introduced in #267 rather than paralleling them:
`EMBEDDINGS_BASE_URL` and `EMBEDDINGS_API_KEY` fall back to `LLM_BASE_URL` and
`LLM_API_KEY`, and `EMBEDDINGS_MODEL` uses the same `model[?param=value&...]` spec,
parsed at startup. The model — not the URL — is the feature switch, because the URL now
always has a value. The query instruction prefix stays a separate setting: spec
parameters are merged into the request body, and the prefix is applied client-side to
the input text, so carrying it in the spec would send a field no provider accepts.
```

- [ ] **Step 6: Verify the docs build**

Run: `uv run --group docs mkdocs build --strict`
(mkdocs lives in the optional `docs` dependency group, so a bare `uv run mkdocs` fails.)
Expected: build succeeds with no warnings about broken links or missing anchors.

- [ ] **Step 7: Commit**

```bash
git add docs example.env AGENTS.md
git commit -m "docs: document the embedding endpoint alongside the LLM endpoint"
```

- [ ] **Step 8: Refresh the PR description**

The current description still documents `EMBEDDING_BACKEND=openai|ollama`,
`EMBEDDING_PROVIDER_PATH`, a spec at `docs/superpowers/specs/2026-05-28-hybrid-search.md`
and "73 passed" — none of which are true any more. Rewrite the **Configuration** section
to the settings above, fix the spec link to `docs/superpowers/specs/hybrid-search.md`,
and re-run the suite to get a current count:

```bash
uv run cli test -- radis/pgsearch/ radis/core/ radis/search/ radis/reports/ -q | tail -3
```

Write the new body to a scratch file, then:

```bash
gh pr edit 226 --body-file /tmp/pr-226-body.md
```

---

## Self-Review

**Spec coverage.** The six alignment items raised in the review are each covered: URL/key
inheritance and the `ModelSpec` switch (Task 2), plural naming (Task 1), the missing
search-path guard and the three duplicated ones (Task 3), the `dimensions`/`EMBEDDINGS_DIM`
conflict this introduces (Task 4), client placement (Task 5), and the documentation gap
plus the stale PR body (Task 6).

**Deliberately not in scope.** Two ideas from the discussion are excluded, and should stay
excluded unless the maintainer asks:
- *Keying the rate-limit gate by base URL* so a shared gateway backs off once for both
  services. Real, but only bites deployments that point both at one quota, and it changes
  behavior for LLM callers who are not part of this PR.
- *Making the `EMBEDDINGS_RATE_LIMIT_*` knobs env-readable* like their LLM counterparts.
  Task 1 renames them; turning constants into settings is a separate, easily-reviewed
  change and does not block the merge.

**Type consistency.** `EMBEDDINGS_MODEL` is `ModelSpec | None` everywhere: produced in
Task 2, consumed as `spec.model` / `spec.params` by the client (Task 2) and the cache
fingerprint (Task 2), tested for `is None` in Task 3, and read for `params["dimensions"]`
in Task 4. Every reader that can run before Task 3's caller guard exists handles `None`
itself, so no intermediate commit leaves the suite red. Tests construct it with `parse_model_spec("…")` rather than a bare string —
`override_settings(EMBEDDINGS_MODEL="qwen3")` would pass a `str` where the code expects a
`ModelSpec` and fail with `AttributeError` on `.model`.

**Ordering constraint.** Task 1 (the rename) comes first so no later task writes a name
that is about to change. Task 2 must land before Tasks 3 and 4, which read
`EMBEDDINGS_MODEL`. Task 5
is independent of Tasks 2–4. Task 6 must land last, since it documents the final names.
