# Search Modes Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace hybrid RRF fusion with two search modes, full-text (default, with an optional similarity re-rank of the top hits by a typed or LLM-written description) and semantic, chosen on the search page.

**Architecture:** One provider entry point `search(request)` over a typed request hierarchy (`FullTextSearch`, `SemanticSearch`) in `radis/search/site.py`; the pgsearch provider ranks each mode separately behind a two-layer ranking cache; the search page gets a mode select, a description textarea with an HTMX "Write from keywords" button backed by an async LLM endpoint, and a semantic cutoff field. The summary excerpt is escaped on the way out.

**Tech Stack:** Django 6.1, PostgreSQL 17 with pgvector (HNSW, `hnsw.iterative_scan`), django-crispy-forms, HTMX 1.9 and Alpine 3.15 (vendored by adit-radis-shared), the openai SDK through `radis.core.utils.llm_client.AsyncChatClient` and `radis.core.utils.embedding_client.EmbeddingClient`, pytest with pytest-django and pytest-asyncio (strict mode), ruff, pyright (basic), djlint.

**Spec:** `docs/superpowers/specs/2026-10-22-search-modes-design.md` (approved 2026-10-22). Every task argues from it; read the spec section a task names before starting it.

## Global Constraints

- Work in the worktree `/projects/deploy/radis_staging/.claude/worktrees/search-modes` on branch `search-modes` (from `main` at 8ed9ea3e). Never `cd` to the main checkout.
- Run tests as `DJANGO_SETTINGS_MODULE=radis.settings.test uv run pytest -p no:cacheprovider <path> -v` from the worktree; its `.env` points at the dedicated test database (`POSTGRES_DEV_PORT=55432`). Under these settings `EMBEDDINGS_MODEL` is `None`; every test that needs an embedding model sets `settings.EMBEDDINGS_MODEL = parse_model_spec("qwen3")` and patches `radis.pgsearch.providers.EmbeddingClient`; every test of no-model behaviour sets `settings.EMBEDDINGS_MODEL = None` explicitly.
- Lint gate: `uv run cli lint` (ruff with E, F, I, DJ, UP; pyright basic with `reportUnnecessaryTypeIgnoreComment`; djlint). Run it before every commit; the pre-commit hooks run the same checks, do not bypass them.
- Warnings are errors in pytest (`filterwarnings = error`); async tests carry `@pytest.mark.asyncio`; no async fixtures.
- Comments explain why, never history ("Task N adds…", "previously…" are forbidden); docstrings describe the code as it is.
- Copy these strings verbatim from the spec: full-text placeholder "Optional: describe the reports you are looking for; the top hits are re-ranked by it"; semantic placeholder "Describe the reports you are looking for"; button "Write from keywords"; cutoff label "Minimum similarity"; notices "Full-text mode needs keywords. Switch to Semantic to search by description alone.", "Semantic mode needs a description.", "Enter search keywords first; the description is written from the positive terms.", "The LLM could not write a description right now.", "Your description could not be applied (embedding service unavailable); results are in keyword order. Search again to retry.", "Semantic search is unavailable right now."
- Settings (spec §7), exact names and defaults: `SEARCH_FTS_MAX_RESULTS = 10_000`; `SEARCH_HNSW_EF_SEARCH` env int default 500 in 1..1000; `SEARCH_RANKING_CACHE_TIMEOUT_SECONDS` env int default 300; `SEARCH_RERANK_WINDOW` env int default 1000, ≥ 1; `SEARCH_RERANK_SEMANTIC_WEIGHT` env float default 0.7 in 0..1; `SEARCH_SEMANTIC_ITERATIVE_SCAN` env bool default True; `SEARCH_SEMANTIC_MIN_SIMILARITY` env float default None in 0..1; `EMBEDDINGS_QUERY_REQUEST_TIMEOUT_SECONDS` env float default 10.0; `SEARCH_DESCRIPTION_SYSTEM_PROMPT` env str; `SEARCH_DESCRIPTION_TIMEOUT` env int default 10; `SEARCH_DESCRIPTION_MAX_TOKENS` env int default 200; `SEARCH_DESCRIPTION_CACHE_TIMEOUT_SECONDS` env int default 3600. `HYBRID_*` names disappear entirely (`git grep HYBRID_` must be empty at the end of Task 2).
- Every commit message is an imperative sentence like the repository's history ("Add the search modes design spec"), with a body when the change needs one, and ends with these two lines:

  ```
  Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>
  Claude-Session: https://claude.ai/code/session_01XTntToXdCSFQYyhezJdYvc
  ```

- Never read report rows or bodies from the staging database; staging measurements (Task 12) record ids and numbers only.

## Review Focus

Input classes the spec implies but no section spells out; each has its pinning test in the task that owns the code:

1. A description longer than 1,000 characters (pasted text) must be refused by the form with a visible error, never silently truncated or run — Task 7, `test_description_over_the_limit_is_invalid`.
2. A cutoff outside 0..1 (`min_similarity=1.5` or `-0.1` in the URL) must be a form error, not a 500 from `SemanticSearch.__post_init__` — Task 7, `test_min_similarity_outside_unit_interval_is_invalid`.
3. A bookmarked `?mode=semantic&description=…` URL opened on a deployment without an embedding model must run as a plain full-text search of the keywords (or show the info card without keywords), never raise — Task 8, `test_semantic_url_without_the_capability_runs_full_text`.
4. Page 3 of a semantic list with two hits (stale pagination link) must render page 1's clamp, not a 404 — Task 8, `test_page_beyond_a_short_semantic_list_is_clamped`.
5. The write button with a query that parses to nothing (`"" AND ()`) must answer the "enter keywords" notice with no LLM call — Task 10, `test_unparseable_keywords_get_the_enter_keywords_notice`.

## File Structure

Created:
- `radis/pgsearch/utils/ranking.py` — pure window re-rank (`rerank_window`, `min_max`), no database access.
- `radis/pgsearch/tests/test_ranking.py`, `test_document_utils.py`, `test_provider_fulltext.py`, `test_provider_semantic.py`, `test_ranking_cache.py`, `test_search_settings.py`.
- `radis/search/utils/description.py` — the async LLM description writer.
- `radis/search/templates/search/form_elements/description_input.html` — form-free textarea partial shared by the page and the endpoint.
- `radis/search/tests/conftest.py`, `test_site.py`, `test_description.py`, `test_description_view.py`.

Modified (one responsibility each):
- `radis/search/site.py` — request and result types, provider contract.
- `radis/search/forms.py` — `SearchForm` fields and layouts, `DescriptionRequestForm`.
- `radis/search/layouts.py`, `templates/search/form_elements/query_input.html` — the query row.
- `radis/search/views.py` — `SearchView` mode dispatch, `description_view`.
- `radis/search/urls.py` — the endpoint route.
- `radis/search/templates/search/_search_results.html`, `_result_header.html`, `_result_document.html`, `_search_info.html`, `search.html` — per-mode rendering.
- `radis/search/static/search/search.js` — mode state.
- `radis/search/utils/query_parser.py` — `unparse_positive_terms` (rename).
- `radis/pgsearch/providers.py` — full-text and semantic ranking, ranking cache, hydration.
- `radis/pgsearch/utils/document_utils.py` — `summary_markup`, document builder.
- `radis/pgsearch/apps.py` — registration with `semantic`.
- `radis/core/utils/embedding_client.py` — `timeout` keyword.
- `radis/settings/base.py`, `radis/settings/production.py`, `example.env` — settings.
- `radis/extractions/site.py`, `forms.py`, `tasks.py`, `views.py` — `FullTextSearch` call sites.
- Documentation listed in spec §8.

Deleted: `radis/pgsearch/utils/fusion.py`, `radis/pgsearch/tests/test_fusion.py`, `test_negative_query_string.py`, `test_provider_hybrid.py`, `test_fused_cache.py`, `radis/search/tests/test_query_parser_unparse_for_embedding.py` (renamed).

---

### Task 1: Request and result types

Spec §3. The provider keeps today's fusion behaviour in this task; only its signatures and the call sites change, so every existing test still passes.

**Files:**
- Modify: `radis/search/site.py` (whole file)
- Modify: `radis/extractions/site.py:1-22`
- Modify: `radis/extractions/forms.py:18,135`, `radis/extractions/tasks.py:10,83`, `radis/extractions/views.py:50,411`
- Modify: `radis/pgsearch/providers.py` (import line 25; signatures at lines 409, 467, 490, 567, 651, 659; the `document_from_pgsearch_response` call at 641-645)
- Modify: `radis/pgsearch/utils/document_utils.py:13-31`
- Modify: `radis/pgsearch/tests/test_fused_cache.py:15,47-55`, `test_language_config.py:8-24`, `test_provider_hybrid.py:15,48-59,225-243,258-263,299-301,338-340`, `test_providers.py:31,76-88,417-424`, `test_query_embedding_cache.py:13,33-45`
- Test: `radis/search/tests/test_site.py` (new)

**Interfaces:**
- Produces: `radis.search.site.Search` (frozen kw-only dataclass base: `filters: SearchFilters`, `offset: int = 0`, `limit: int | None = 10`), `SimilarityRerank(text: str)`, `FullTextSearch(Search)` with `query: QueryNode`, `rerank: SimilarityRerank | None = None`, `SemanticSearch(Search)` with `text: str`, `min_similarity: float | None = None`, `SearchRequest = FullTextSearch | SemanticSearch`, `SearchProvider(name, search: Callable[[SearchRequest], SearchResult], max_results: int, semantic: bool = False)`, `SearchResult(total_count, total_relation, documents, degraded: bool = False)`, `ReportDocument(..., summary, similarity: float | None = None, score: float = 0.0)`; `radis.pgsearch.utils.document_utils.document_from_pgsearch_response(record, similarity=None, score=0.0)`.

- [ ] **Step 1: Write the failing type tests**

Create `radis/search/tests/test_site.py`:

```python
import dataclasses

import pytest

from radis.search.site import (
    FullTextSearch,
    SearchFilters,
    SearchProvider,
    SearchResult,
    SemanticSearch,
    SimilarityRerank,
)
from radis.search.utils.query_parser import QueryParser


def _node(text: str):
    node, _fixes = QueryParser().parse(text)
    assert node is not None
    return node


def test_full_text_search_defaults():
    request = FullTextSearch(query=_node("pneumothorax"), filters=SearchFilters(group=1))
    assert request.offset == 0
    assert request.limit == 10
    assert request.rerank is None


def test_requests_are_keyword_only():
    with pytest.raises(TypeError):
        FullTextSearch(_node("pneumothorax"), SearchFilters(group=1))  # type: ignore[misc]


def test_requests_are_frozen():
    request = SemanticSearch(text="free air", filters=SearchFilters(group=1))
    with pytest.raises(dataclasses.FrozenInstanceError):
        request.text = "other"  # type: ignore[misc]


def test_blank_rerank_text_is_rejected():
    with pytest.raises(ValueError):
        SimilarityRerank("   ")


def test_blank_semantic_text_is_rejected():
    with pytest.raises(ValueError):
        SemanticSearch(text=" \n", filters=SearchFilters(group=1))


@pytest.mark.parametrize("value", [-0.1, 1.5])
def test_min_similarity_outside_the_unit_interval_is_rejected(value):
    with pytest.raises(ValueError):
        SemanticSearch(text="free air", filters=SearchFilters(group=1), min_similarity=value)


def test_min_similarity_bounds_are_accepted():
    assert SemanticSearch(text="x", filters=SearchFilters(), min_similarity=0.0).min_similarity == 0.0
    assert SemanticSearch(text="x", filters=SearchFilters(), min_similarity=1.0).min_similarity == 1.0


def test_result_and_provider_defaults_keep_stubs_constructible():
    result = SearchResult(total_count=0, total_relation="exact", documents=[])
    assert result.degraded is False
    provider = SearchProvider(name="stub", search=lambda request: result, max_results=10)
    assert provider.semantic is False
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `DJANGO_SETTINGS_MODULE=radis.settings.test uv run pytest -p no:cacheprovider radis/search/tests/test_site.py -v`
Expected: FAIL with `ImportError: cannot import name 'FullTextSearch'`.

- [ ] **Step 3: Rewrite `radis/search/site.py`**

Replace the `ReportDocument`, `SearchResult`, `Search` and `SearchProvider` definitions (keep `SearchFilters`, `search_provider` and `register_search_provider` as they are):

```python
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Literal, NamedTuple

from radis.reports.models import Report
from radis.search.utils.query_parser import QueryNode


class ReportDocument(NamedTuple):
    relevance: float | None  # ts_rank of a full-text hit; None in semantic mode
    document_id: str
    pacs_name: str
    pacs_link: str
    patient_age: int
    patient_sex: Literal["F", "M", "U"]
    study_description: str
    modalities: list[str]
    # HTML-safe markup: escaped report text plus <em> highlights, marked safe by the
    # provider. A plain string from another provider is autoescaped by the template.
    summary: str
    similarity: float | None = None  # 1 - cosine distance when measured, else None
    score: float = 0.0  # the value that decided the position in the result list

    @property
    def full_report(self) -> Report:
        return Report.objects.get(document_id=self.document_id)


class SearchResult(NamedTuple):
    total_count: int
    total_relation: Literal["exact", "at_least", "approximately"]
    documents: list[ReportDocument]
    # True when the semantic part of the search (the re-rank, or semantic mode itself)
    # could not run because no embedding was available.
    degraded: bool = False
```

Below `SearchFilters` (unchanged), replace the `Search` NamedTuple with:

```python
@dataclass(frozen=True, kw_only=True)
class Search:
    """Inputs every search mode shares.

    Offset and limit both 0 ask the provider for the most accurate total count it
    can give; extractions rely on this.
    """

    filters: SearchFilters
    offset: int = 0
    limit: int | None = 10


@dataclass(frozen=True)
class SimilarityRerank:
    """Re-order the top SEARCH_RERANK_WINDOW keyword hits by a blend of their
    similarity to ``text`` and their keyword rank."""

    text: str

    def __post_init__(self) -> None:
        if not self.text.strip():
            raise ValueError("The re-rank text must not be blank.")


@dataclass(frozen=True, kw_only=True)
class FullTextSearch(Search):
    """Exactly the keyword hits, optionally re-ranked within the window."""

    query: QueryNode
    rerank: SimilarityRerank | None = None


@dataclass(frozen=True, kw_only=True)
class SemanticSearch(Search):
    """The reports most similar to ``text``, in similarity order."""

    text: str
    # Overrides SEARCH_SEMANTIC_MIN_SIMILARITY for this search; None means the setting.
    min_similarity: float | None = None

    def __post_init__(self) -> None:
        if not self.text.strip():
            raise ValueError("The semantic search text must not be blank.")
        if self.min_similarity is not None and not 0.0 <= self.min_similarity <= 1.0:
            raise ValueError("min_similarity must be between 0 and 1.")


SearchRequest = FullTextSearch | SemanticSearch


class SearchProvider(NamedTuple):
    """A registered search backend.

    Attributes:
    - name: The name of the search provider.
    - search: Runs one request of either mode.
    - max_results: The paging cap of a full-text list; offset + limit must stay below it.
    - semantic: Whether SemanticSearch requests and re-ranks are available (an
      embedding model is configured).
    """

    name: str
    search: Callable[[SearchRequest], SearchResult]
    max_results: int
    semantic: bool = False
```

- [ ] **Step 4: Retype the extraction registry and its three call sites**

`radis/extractions/site.py`: change the import to `from radis.search.site import FullTextSearch` and both callables to `count: Callable[[FullTextSearch], int]` and `retrieve: Callable[[FullTextSearch], Iterable[str]]`.

In `radis/extractions/forms.py`, `tasks.py` and `views.py`: change `from radis.search.site import Search, SearchFilters` to `from radis.search.site import FullTextSearch, SearchFilters` and the constructor `Search(` to `FullTextSearch(` (the keyword arguments `query=`, `offset=`, `limit=`, `filters=` stay as they are).

- [ ] **Step 5: Retype the provider and the document builder without changing behaviour**

In `radis/pgsearch/providers.py`:
- import `FullTextSearch` instead of `Search` (line 25) and change every `search: Search` annotation (`_fused_cache_key`, `_fuse_hybrid`, `_fuse_hybrid_uncached`, `search`, `count`, `retrieve`) to `search: FullTextSearch`;
- at the `document_from_pgsearch_response(...)` call (lines 641-645) pass `similarity=(1.0 - dist) if (dist := vec_distance.get(rid)) is not None else None, score=rrf_score_by_id.get(rid, 0.0)` instead of `cosine_distance=`/`rrf_score=`.

In `radis/pgsearch/utils/document_utils.py` change the builder's signature and body:

```python
def document_from_pgsearch_response(
    record: AnnotatedReportSearchIndex,
    similarity: float | None = None,
    score: float = 0.0,
) -> ReportDocument:
    report = record.report
    return ReportDocument(
        relevance=record.rank,
        document_id=report.document_id,
        pacs_name=report.pacs_name,
        pacs_link=report.pacs_link,
        patient_age=report.patient_age,
        patient_sex=report.patient_sex,
        study_description=report.study_description,
        modalities=report.modality_codes,
        summary=record.summary,
        similarity=similarity,
        score=score,
    )
```

- [ ] **Step 6: Update the pgsearch test helpers**

In each of `test_fused_cache.py`, `test_language_config.py`, `test_provider_hybrid.py`, `test_providers.py`, `test_query_embedding_cache.py`: import `FullTextSearch` instead of `Search` and replace every `Search(` constructor and `-> Search` annotation with `FullTextSearch`. Delete `test_documents_carry_cosine_distance_and_rrf_score` (test_provider_hybrid.py lines 225-243); the fields it asserts no longer exist and the fusion it describes goes in Task 5.

- [ ] **Step 7: Run the affected suites**

Run: `DJANGO_SETTINGS_MODULE=radis.settings.test uv run pytest -p no:cacheprovider radis/search radis/pgsearch radis/extractions -q`
Expected: all pass (the new `test_site.py` included).

Run: `uv run cli lint`
Expected: clean. If pyright reports `reportCallIssue` on a `Search(` you missed, fix that call site.

- [ ] **Step 8: Commit**

```bash
git add radis/search/site.py radis/search/tests/test_site.py radis/extractions radis/pgsearch
git commit -m "Type search requests as full-text or semantic" -m "The provider still fuses; only the request types and their call sites change." -m "Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>" -m "Claude-Session: https://claude.ai/code/session_01XTntToXdCSFQYyhezJdYvc"
```

---

### Task 2: Settings, the query-path embedding timeout and the production cache size

Spec §4.3, §4.4 (cache size), §7. The `HYBRID_` family is renamed or removed, the new settings are defined with boot-time validation, `EmbeddingClient` gains a `timeout` keyword, and `example.env` documents everything env-driven. RRF still runs in this task; its constant moves into the provider module until Task 5 deletes it.

**Files:**
- Modify: `radis/settings/base.py:459-461` (next to `EMBEDDINGS_REQUEST_TIMEOUT_SECONDS`), `:540-575` (the hybrid block), after `:674` (`QUERY_GENERATION_MAX_RETRIES`)
- Modify: `radis/settings/production.py:18-23`
- Modify: `radis/core/utils/embedding_client.py:25-28,97-113,152-155`
- Modify: `radis/pgsearch/providers.py` (every `settings.HYBRID_*` reference; `_embed_query_or_none`)
- Modify: `radis/pgsearch/apps.py:168-177`
- Modify: `radis/pgsearch/tests/test_fused_cache.py:176`, `test_provider_hybrid.py:512`, `test_providers.py:117-119`
- Modify: `example.env:161,186-192,203-208` and append after `:208`
- Test: `radis/pgsearch/tests/test_search_settings.py` (new), `radis/core/tests/test_embedding_client.py` (append)

**Interfaces:**
- Produces: the settings named in Global Constraints; `EmbeddingClient(*, timeout: float | None = None)`; `radis.pgsearch.providers.RRF_K = 60` (module constant, deleted in Task 5).

- [ ] **Step 1: Write the failing settings and client tests**

Create `radis/pgsearch/tests/test_search_settings.py`:

```python
from django.conf import settings


def test_search_settings_defaults():
    assert settings.SEARCH_FTS_MAX_RESULTS == 10_000
    assert settings.SEARCH_HNSW_EF_SEARCH == 500
    assert settings.SEARCH_RANKING_CACHE_TIMEOUT_SECONDS == 300
    assert settings.SEARCH_RERANK_WINDOW == 1000
    assert settings.SEARCH_RERANK_SEMANTIC_WEIGHT == 0.7
    assert settings.SEARCH_SEMANTIC_ITERATIVE_SCAN is True
    assert settings.SEARCH_SEMANTIC_MIN_SIMILARITY is None
    assert settings.EMBEDDINGS_QUERY_REQUEST_TIMEOUT_SECONDS == 10.0
    assert settings.SEARCH_DESCRIPTION_TIMEOUT == 10
    assert settings.SEARCH_DESCRIPTION_MAX_TOKENS == 200
    assert settings.SEARCH_DESCRIPTION_CACHE_TIMEOUT_SECONDS == 3600
    assert "radiology reports" in settings.SEARCH_DESCRIPTION_SYSTEM_PROMPT


def test_the_beam_width_rides_on_the_connection_options():
    options = settings.DATABASES["default"]["OPTIONS"]["options"]
    assert options == f"-c hnsw.ef_search={settings.SEARCH_HNSW_EF_SEARCH}"


def test_no_hybrid_setting_remains():
    for name in ("HYBRID_FTS_MAX_RESULTS", "HYBRID_RRF_K", "HYBRID_HNSW_EF_SEARCH",
                 "HYBRID_FUSED_CACHE_TIMEOUT_SECONDS"):
        assert not hasattr(settings, name), name
```

Append to `radis/core/tests/test_embedding_client.py`:

```python
def test_embedding_client_timeout_override(settings):
    from unittest.mock import patch

    from radis.core.utils.model_spec import parse_model_spec

    settings.EMBEDDINGS_MODEL = parse_model_spec("qwen3")
    settings.EMBEDDINGS_REQUEST_TIMEOUT_SECONDS = 42.0
    with patch("openai.OpenAI") as openai_cls:
        EmbeddingClient(timeout=5.0)
        EmbeddingClient()
    assert openai_cls.call_args_list[0].kwargs["timeout"] == 5.0
    assert openai_cls.call_args_list[1].kwargs["timeout"] == 42.0
```

(`EmbeddingClient` is already imported at the top of that module; if not, add `from radis.core.utils.embedding_client import EmbeddingClient`.)

- [ ] **Step 2: Run them to verify they fail**

Run: `DJANGO_SETTINGS_MODULE=radis.settings.test uv run pytest -p no:cacheprovider radis/pgsearch/tests/test_search_settings.py radis/core/tests/test_embedding_client.py -v`
Expected: `AttributeError: 'Settings' object has no attribute 'SEARCH_FTS_MAX_RESULTS'` and `TypeError: __init__() got an unexpected keyword argument 'timeout'`.

- [ ] **Step 3: Replace the hybrid settings block**

In `radis/settings/base.py` replace lines 540-575 (from `# Hybrid search tuning` to the `HYBRID_FUSED_CACHE_TIMEOUT_SECONDS` line) with:

```python
# Search tuning (radis.pgsearch)
# How many keyword hits a full-text search ranks and paginates.
SEARCH_FTS_MAX_RESULTS = 10_000
# pgvector's HNSW beam width, carried as a libpq connection option so every connection
# scans at the same depth, and the number of reports a semantic search lists: one index
# scan yields on the order of ef_search rows and the provider caps its list at exactly
# this value. 1..1000 is the server's valid range; an out-of-range value would fail GUC
# validation at connect time and silently leave the scan at pgvector's default of 40.
SEARCH_HNSW_EF_SEARCH = _optional_env("SEARCH_HNSW_EF_SEARCH", int, 500)
if not 1 <= SEARCH_HNSW_EF_SEARCH <= 1000:
    raise ImproperlyConfigured(
        f"SEARCH_HNSW_EF_SEARCH={SEARCH_HNSW_EF_SEARCH} must be between 1 and 1000, "
        "the server's valid hnsw.ef_search range."
    )
# A connection option rather than a database- or server-level default, so the app
# carries the setting into every environment it connects to (dev, tests, CI, a restored
# database). production.py sets the database password on the same DATABASES entry.
DATABASES["default"].setdefault("OPTIONS", {})["options"] = (
    f"-c hnsw.ef_search={SEARCH_HNSW_EF_SEARCH}"
)
# How long a ranking (ordered ids and scores of a full-text search, its re-ranked
# window, or a semantic list) stays cached, in seconds. Pagination re-runs the search
# for every page and ranking every keyword hit of a common term costs seconds on a
# large corpus, so page 2..n and repeated searches reuse the cached ranking. The trade
# is freshness: reports created or updated within the TTL join a cached query's results
# only after it expires. 0 disables the cache. A ranking whose embedding failed is
# never cached.
SEARCH_RANKING_CACHE_TIMEOUT_SECONDS = _optional_env(
    "SEARCH_RANKING_CACHE_TIMEOUT_SECONDS", int, 300
)
# The window re-rank of full-text mode: how many of the top keyword hits are measured
# against the description (one query reading that many vectors), and the weight of
# similarity in the blended score (1.0 orders the window by similarity alone).
SEARCH_RERANK_WINDOW = _optional_env("SEARCH_RERANK_WINDOW", int, 1000)
if SEARCH_RERANK_WINDOW < 1:
    raise ImproperlyConfigured(f"SEARCH_RERANK_WINDOW={SEARCH_RERANK_WINDOW} must be at least 1.")
SEARCH_RERANK_SEMANTIC_WEIGHT = _optional_env("SEARCH_RERANK_SEMANTIC_WEIGHT", float, 0.7)
if not 0.0 <= SEARCH_RERANK_SEMANTIC_WEIGHT <= 1.0:
    raise ImproperlyConfigured(
        f"SEARCH_RERANK_SEMANTIC_WEIGHT={SEARCH_RERANK_SEMANTIC_WEIGHT} must be between 0 and 1."
    )
# Semantic mode runs its index scan with hnsw.iterative_scan = strict_order, so the scan
# resumes under selective filters (a small group, a rare modality) until the list is full
# or pgvector's scan budget (hnsw.max_scan_tuples, 20,000 by default) is spent, instead of
# listing only the accessible part of one pass. Costs about 2.4x on unfiltered searches.
SEARCH_SEMANTIC_ITERATIVE_SCAN = env.bool("SEARCH_SEMANTIC_ITERATIVE_SCAN", default=True)
# Minimum similarity (1 - cosine distance, 0..1) a report needs to appear in semantic
# mode. Applied after retrieval, so a query that matches nothing ends in a short list
# instead of the nearest 500 reports. Comparable only within one embedding model and
# corpus, hence unset (off) until calibrated; the page lets a user override it per search.
SEARCH_SEMANTIC_MIN_SIMILARITY = _optional_env("SEARCH_SEMANTIC_MIN_SIMILARITY", float, None)
if SEARCH_SEMANTIC_MIN_SIMILARITY is not None and not 0.0 <= SEARCH_SEMANTIC_MIN_SIMILARITY <= 1.0:
    raise ImproperlyConfigured(
        f"SEARCH_SEMANTIC_MIN_SIMILARITY={SEARCH_SEMANTIC_MIN_SIMILARITY} must be between 0 and 1."
    )
```

- [ ] **Step 4: Add the query-path timeout and the description writer settings**

Directly after `EMBEDDINGS_REQUEST_TIMEOUT_SECONDS` (base.py lines 459-461) add:

```python
# A user waits on a query embedding, so the search path gets its own short request
# timeout instead of the worker's; a dead endpoint then costs one search ten seconds,
# not a minute. The gate's query budget (EMBEDDINGS_RATE_LIMIT_QUERY_MAX_WAIT_SECONDS)
# comes on top of it.
EMBEDDINGS_QUERY_REQUEST_TIMEOUT_SECONDS = _optional_env(
    "EMBEDDINGS_QUERY_REQUEST_TIMEOUT_SECONDS", float, 10.0
)
```

Also change the comment at lines 525-526 ("short for the search path, where a user is waiting and falling back to FTS-only beats hanging on a closed gate") to "short for the search path, where a user is waiting and a degraded search beats hanging on a closed gate".

Directly after `QUERY_GENERATION_MAX_RETRIES` (line 674) add:

```python
# The "Write from keywords" button on the search page: the LLM (the query_generation
# feature's model) writes a short report-style passage from the keywords, which the
# semantic search embeds instead of the bare keywords. A user waits on it, so the
# timeout is the gate budget too and there are no transient retries.
SEARCH_DESCRIPTION_TIMEOUT = _optional_env("SEARCH_DESCRIPTION_TIMEOUT", int, 10)
SEARCH_DESCRIPTION_MAX_TOKENS = _optional_env("SEARCH_DESCRIPTION_MAX_TOKENS", int, 200)
SEARCH_DESCRIPTION_CACHE_TIMEOUT_SECONDS = _optional_env(
    "SEARCH_DESCRIPTION_CACHE_TIMEOUT_SECONDS", int, 3600
)
SEARCH_DESCRIPTION_SYSTEM_PROMPT = env.str(
    "SEARCH_DESCRIPTION_SYSTEM_PROMPT",
    default=(
        "You help search a hospital archive of radiology reports. The user gives search "
        "keywords. Write the short passage (two to three sentences) that a radiology report "
        "matching those keywords would typically contain in its findings or impression: the "
        "same language as the keywords, the usual terminology and abbreviations of radiology "
        "reports, no patient names, no headings and no explanation. Output only the passage."
    ),
)
```

- [ ] **Step 5: Size the production cache**

In `radis/settings/production.py` replace the `CACHES` block with:

```python
CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.db.DatabaseCache",
        "LOCATION": "django_cache",
        # One row per cached ranking, query vector and LLM-written description; Django's
        # default of 300 entries culls a third of the table at random once it is full,
        # which would evict hot rankings under modest traffic. The TTLs bound the table.
        "OPTIONS": {"MAX_ENTRIES": 2000},
    }
}
```

(Keep the existing comment above the block about the shared cache across replicas.)

- [ ] **Step 6: Give the embedding client a timeout keyword and reword its messages**

In `radis/core/utils/embedding_client.py`:
- `def __init__(self, *, timeout: float | None = None) -> None:` and in the `openai.OpenAI(...)` call `timeout=timeout if timeout is not None else settings.EMBEDDINGS_REQUEST_TIMEOUT_SECONDS,`. Add to the class docstring: "``timeout`` overrides the per-request HTTP timeout; the search path passes the short query-path timeout."
- The error message becomes `"EMBEDDINGS_MODEL is not configured; semantic search is disabled"`.
- Lines 25-28: replace "(read path) fall back to FTS-only" with "(read path) report the search as degraded".
- `embed_query` docstring: replace "and the provider falls back to FTS-only" with "and the provider reports the search as degraded".

- [ ] **Step 7: Rename the provider's settings references**

In `radis/pgsearch/providers.py`:
- add `RRF_K = 60` below the module logger (one line, no comment) and replace both `settings.HYBRID_RRF_K` uses with `RRF_K`;
- replace `settings.HYBRID_FTS_MAX_RESULTS` with `settings.SEARCH_FTS_MAX_RESULTS` (three places), `settings.HYBRID_HNSW_EF_SEARCH` with `settings.SEARCH_HNSW_EF_SEARCH` (two places) and `settings.HYBRID_FUSED_CACHE_TIMEOUT_SECONDS` with `settings.SEARCH_RANKING_CACHE_TIMEOUT_SECONDS`;
- in `_embed_query_or_none` change `with EmbeddingClient() as ec:` to `with EmbeddingClient(timeout=settings.EMBEDDINGS_QUERY_REQUEST_TIMEOUT_SECONDS) as ec:`.

In `radis/pgsearch/apps.py:175` use `settings.SEARCH_FTS_MAX_RESULTS`. In `test_fused_cache.py:176` set `settings.SEARCH_RANKING_CACHE_TIMEOUT_SECONDS = 0`; in `test_provider_hybrid.py:512` compare with `settings.SEARCH_HNSW_EF_SEARCH`; in `test_providers.py:117-119` name `SEARCH_RANKING_CACHE_TIMEOUT_SECONDS`.

Run: `git grep -n HYBRID_ -- 'radis/**/*.py'`
Expected: no output.

- [ ] **Step 8: Document the settings in `example.env`**

Change line 161 to `# Embedding service (OpenAI-compatible /v1/embeddings), used by semantic search and the`
followed by `# similarity re-rank of full-text hits.`; change line 186 to `# Instruction prefix prepended to the description text before embedding. Model-specific:`. Append after line 208:

```
# Request timeout of a query embedding on the search path, in seconds (a user is
# waiting). Report embedding keeps EMBEDDINGS_REQUEST_TIMEOUT_SECONDS.
#EMBEDDINGS_QUERY_REQUEST_TIMEOUT_SECONDS=10

# Search tuning (radis.pgsearch). All optional.
# Beam width of the HNSW scan and the number of reports a semantic search lists (1..1000).
#SEARCH_HNSW_EF_SEARCH=500
# How long a ranking (ordered ids and scores) stays cached, in seconds; 0 disables.
#SEARCH_RANKING_CACHE_TIMEOUT_SECONDS=300
# Full-text hits re-ranked when a description is given, and the weight of similarity
# in the re-rank score (0..1; 1 orders the window by similarity alone).
#SEARCH_RERANK_WINDOW=1000
#SEARCH_RERANK_SEMANTIC_WEIGHT=0.7
# Let a semantic search resume the index scan under selective filters
# (hnsw.iterative_scan = strict_order) instead of listing only the accessible part
# of one pass. Costs about 2.4x on unfiltered searches.
#SEARCH_SEMANTIC_ITERATIVE_SCAN=true
# Minimum similarity (0..1) a report needs to appear in semantic mode; unset is off.
# Users can override it per search.
#SEARCH_SEMANTIC_MIN_SIMILARITY=

# The "Write from keywords" button on the search page asks the LLM (the
# query_generation feature's model, LLM_QUERY_GENERATION_MODEL) for a short
# report-style description. Timeout in seconds, answer cap in tokens, cache in seconds.
#SEARCH_DESCRIPTION_TIMEOUT=10
#SEARCH_DESCRIPTION_MAX_TOKENS=200
#SEARCH_DESCRIPTION_CACHE_TIMEOUT_SECONDS=3600
#SEARCH_DESCRIPTION_SYSTEM_PROMPT=
```

- [ ] **Step 9: Run the tests and the lint**

Run: `DJANGO_SETTINGS_MODULE=radis.settings.test uv run pytest -p no:cacheprovider radis/pgsearch radis/core/tests/test_embedding_client.py radis/core/tests/test_embeddings_settings.py -q`
Expected: all pass.

Run: `uv run cli lint`
Expected: clean.

- [ ] **Step 10: Commit**

```bash
git add radis/settings example.env radis/core/utils/embedding_client.py radis/core/tests/test_embedding_client.py radis/pgsearch
git commit -m "Rename the search settings and bound the query embedding wait" -m "The HYBRID_ family becomes SEARCH_, the new ranking, cutoff and description-writer settings get boot-time validation, a query embedding times out after ten seconds, and the production cache holds more than 300 rows." -m "Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>" -m "Claude-Session: https://claude.ai/code/session_01XTntToXdCSFQYyhezJdYvc"
```

---

### Task 3: The window re-rank as a pure function

Spec §4.2 steps 4 and 5. No database, no settings: a function over ranked lists, so the scoring is unit-tested exactly.

**Files:**
- Create: `radis/pgsearch/utils/ranking.py`
- Test: `radis/pgsearch/tests/test_ranking.py`

**Interfaces:**
- Produces: `min_max(values: Mapping[int, float]) -> dict[int, float]` and `rerank_window(hits: Sequence[tuple[int, float]], similarity_by_id: Mapping[int, float], window: int, weight: float) -> list[tuple[int, float]]`, where `hits` are `(report_id, ts_rank)` pairs in keyword order and the result is `(report_id, score)` pairs in result order.

- [ ] **Step 1: Write the failing tests**

Create `radis/pgsearch/tests/test_ranking.py`:

```python
import pytest

from radis.pgsearch.utils.ranking import min_max, rerank_window

HITS = [(1, 0.9), (2, 0.5), (3, 0.1), (4, 0.05), (5, 0.01)]


def _order(pairs):
    return [rid for rid, _ in pairs]


def test_min_max_scales_into_the_unit_interval():
    assert min_max({1: 0.1, 2: 0.3, 3: 0.5}) == {1: 0.0, 2: pytest.approx(0.5), 3: 1.0}


def test_min_max_of_a_constant_input_is_one():
    assert min_max({1: 0.4, 2: 0.4}) == {1: 1.0, 2: 1.0}
    assert min_max({7: 0.0}) == {7: 1.0}
    assert min_max({}) == {}


def test_measured_window_hits_order_by_the_blended_score():
    # norm similarity: 1 -> 0, 2 -> 1, 3 -> 0.571; norm ts_rank: 1 -> 1, 2 -> 0.5, 3 -> 0
    # scores at w=0.7: 1 -> 0.30, 2 -> 0.85, 3 -> 0.40
    sims = {1: 0.2, 2: 0.9, 3: 0.6}
    ordered = rerank_window(HITS[:3], sims, window=3, weight=0.7)
    assert _order(ordered) == [2, 3, 1]
    assert dict(ordered)[2] == pytest.approx(0.85)
    assert dict(ordered)[1] == pytest.approx(0.30)


def test_weight_one_is_pure_similarity_order():
    sims = {1: 0.2, 2: 0.9, 3: 0.6}
    assert _order(rerank_window(HITS[:3], sims, window=3, weight=1.0)) == [2, 3, 1]


def test_weight_zero_keeps_the_keyword_order():
    sims = {1: 0.2, 2: 0.9, 3: 0.6}
    assert _order(rerank_window(HITS[:3], sims, window=3, weight=0.0)) == [1, 2, 3]


def test_hits_beyond_the_window_follow_in_keyword_order_with_their_ts_rank():
    sims = {1: 0.2, 2: 0.9}
    ordered = rerank_window(HITS, sims, window=2, weight=0.7)
    assert _order(ordered) == [2, 1, 3, 4, 5]
    assert ordered[2:] == [(3, 0.1), (4, 0.05), (5, 0.01)]


def test_window_hits_without_a_similarity_follow_the_measured_block_in_keyword_order():
    # Report 1 sits in the window but has no vector: it is not scored and keeps its
    # keyword position relative to the other unscored hits.
    sims = {2: 0.9, 3: 0.6}
    ordered = rerank_window(HITS, sims, window=3, weight=0.7)
    assert _order(ordered) == [2, 3, 1, 4, 5]
    assert dict(ordered)[1] == 0.9


def test_all_unmeasured_is_the_keyword_order():
    assert rerank_window(HITS, {}, window=3, weight=0.7) == HITS


def test_a_window_of_one_scores_one():
    assert rerank_window(HITS[:1], {1: 0.3}, window=1, weight=0.7) == [(1, pytest.approx(1.0))]


def test_ties_break_by_ts_rank_then_id():
    sims = {1: 0.5, 2: 0.5, 3: 0.5}
    ordered = rerank_window([(3, 0.4), (1, 0.4), (2, 0.4)], sims, window=3, weight=0.7)
    assert _order(ordered) == [1, 2, 3]


def test_empty_input():
    assert rerank_window([], {}, window=10, weight=0.7) == []


def test_window_larger_than_the_list():
    sims = {1: 0.1, 2: 0.9}
    assert _order(rerank_window(HITS[:2], sims, window=1000, weight=0.7)) == [2, 1]
```

- [ ] **Step 2: Run them to verify they fail**

Run: `DJANGO_SETTINGS_MODULE=radis.settings.test uv run pytest -p no:cacheprovider radis/pgsearch/tests/test_ranking.py -v`
Expected: `ModuleNotFoundError: No module named 'radis.pgsearch.utils.ranking'`.

- [ ] **Step 3: Write the module**

Create `radis/pgsearch/utils/ranking.py`:

```python
"""The window re-rank of full-text mode, as a pure function over ranked lists."""

from collections.abc import Mapping, Sequence


def min_max(values: Mapping[int, float]) -> dict[int, float]:
    """Scale ``values`` into 0..1. A constant input (one value, or equal values)
    scales to 1.0 everywhere, so a lone hit is not punished for having no peers."""
    if not values:
        return {}
    lo = min(values.values())
    hi = max(values.values())
    if hi == lo:
        return {key: 1.0 for key in values}
    return {key: (value - lo) / (hi - lo) for key, value in values.items()}


def rerank_window(
    hits: Sequence[tuple[int, float]],
    similarity_by_id: Mapping[int, float],
    window: int,
    weight: float,
) -> list[tuple[int, float]]:
    """Order ``hits``, ``(report_id, ts_rank)`` pairs in keyword order, for the page.

    The measured window hits (the first ``window`` hits that have a similarity)
    come first, ordered by ``weight * norm(similarity) + (1 - weight) *
    norm(ts_rank)`` with both signals min-max scaled over those hits, ties broken
    by ts_rank then id. Every other hit follows in keyword order with its ts_rank
    as score: the hits beyond the window, and the window hits that have no
    similarity because they are not embedded yet.
    """
    measured = [(rid, rank) for rid, rank in hits[:window] if rid in similarity_by_id]
    similarity_norm = min_max({rid: similarity_by_id[rid] for rid, _ in measured})
    rank_norm = min_max({rid: rank for rid, rank in measured})
    scored = [
        (rid, weight * similarity_norm[rid] + (1.0 - weight) * rank_norm[rid], rank)
        for rid, rank in measured
    ]
    scored.sort(key=lambda item: (-item[1], -item[2], item[0]))
    measured_ids = {rid for rid, _ in measured}
    ordered = [(rid, score) for rid, score, _ in scored]
    ordered.extend((rid, rank) for rid, rank in hits if rid not in measured_ids)
    return ordered
```

- [ ] **Step 4: Run the tests**

Run: `DJANGO_SETTINGS_MODULE=radis.settings.test uv run pytest -p no:cacheprovider radis/pgsearch/tests/test_ranking.py -v`
Expected: all pass. Run `uv run cli lint`; expected clean.

- [ ] **Step 5: Commit**

```bash
git add radis/pgsearch/utils/ranking.py radis/pgsearch/tests/test_ranking.py
git commit -m "Add the window re-rank as a pure function" -m "Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>" -m "Claude-Session: https://claude.ai/code/session_01XTntToXdCSFQYyhezJdYvc"
```

---

### Task 4: Escape the summary excerpt

Spec §4.6. `ts_headline` output and the body-head fallback become HTML-safe markup built in Python from sentinel markers; the template stops marking report text safe. Fusion still runs in this task; only the hydration loop and the document builder change.

**Files:**
- Modify: `radis/pgsearch/utils/document_utils.py` (whole file)
- Modify: `radis/pgsearch/utils/fusion.py:27-33` (delete `summary_with_fallback`)
- Modify: `radis/pgsearch/providers.py:38` (import), `:586-596` (headline markers), `:619-646` (hydration loop)
- Modify: `radis/search/templates/search/_result_document.html:5`
- Modify: `radis/pgsearch/tests/test_fusion.py:3,59-74` (summary tests move out)
- Modify: `radis/pgsearch/tests/test_language_config.py:101-109` (comment names the helper)
- Test: `radis/pgsearch/tests/test_document_utils.py` (new), `radis/pgsearch/tests/test_providers.py` (append), `radis/search/tests/test_views.py` (append)

**Interfaces:**
- Produces: `radis.pgsearch.utils.document_utils.HEADLINE_START = "\x02"`, `HEADLINE_STOP = "\x03"`, `summary_markup(body: str, headline: str, max_words: int = 30) -> SafeString`, `AnnotatedReportSearchIndex` with `rank: float | None` and `headline: str`, `document_from_pgsearch_response(record: ReportSearchIndex, *, relevance: float | None, summary: str, similarity: float | None, score: float) -> ReportDocument`.

- [ ] **Step 1: Write the failing helper tests**

Create `radis/pgsearch/tests/test_document_utils.py`:

```python
from django.utils.safestring import SafeString

from radis.pgsearch.utils.document_utils import HEADLINE_START, HEADLINE_STOP, summary_markup


def _marked(word: str) -> str:
    return f"{HEADLINE_START}{word}{HEADLINE_STOP}"


def test_headline_markers_become_em_tags():
    out = summary_markup("any body", f"an {_marked('existing')} headline")
    assert out == "an <em>existing</em> headline"
    assert isinstance(out, SafeString)


def test_empty_headline_falls_back_to_the_body_head():
    body = " ".join(f"word{i}" for i in range(100))
    assert summary_markup(body, "", max_words=5) == "word0 word1 word2 word3 word4"


def test_short_body_is_returned_whole():
    assert summary_markup("only three words here", "", max_words=10) == "only three words here"


def test_body_head_is_escaped():
    out = summary_markup("<script>alert(1)</script> & more", "")
    assert out == "&lt;script&gt;alert(1)&lt;/script&gt; &amp; more"


def test_headline_text_is_escaped_around_intact_highlights():
    out = summary_markup("ignored", f"size <5mm {_marked('nodule')} & {_marked('mass')}")
    assert out == "size &lt;5mm <em>nodule</em> &amp; <em>mass</em>"


def test_a_literal_em_tag_in_the_report_stays_escaped():
    assert summary_markup("<em>not a highlight</em>", "") == "&lt;em&gt;not a highlight&lt;/em&gt;"
    assert summary_markup("ignored", f"<em>x</em> {_marked('y')}") == "&lt;em&gt;x&lt;/em&gt; <em>y</em>"


def test_stray_markers_are_dropped_not_rendered():
    assert summary_markup(f"a {HEADLINE_START}b c", "") == "a b c"
    assert summary_markup("ignored", f"a {HEADLINE_STOP}b {_marked('c')}") == "a b <em>c</em>"
    assert "<em>" not in summary_markup("ignored", f"{HEADLINE_START}unclosed")


def test_whitespace_in_the_body_head_is_collapsed():
    assert summary_markup("a\n\n  b\tc", "", max_words=3) == "a b c"
```

- [ ] **Step 2: Run them to verify they fail**

Run: `DJANGO_SETTINGS_MODULE=radis.settings.test uv run pytest -p no:cacheprovider radis/pgsearch/tests/test_document_utils.py -v`
Expected: `ImportError: cannot import name 'HEADLINE_START'`.

- [ ] **Step 3: Rewrite `document_utils.py`**

```python
import re

from django.utils.html import escape
from django.utils.safestring import SafeString, mark_safe

from radis.pgsearch.models import ReportSearchIndex
from radis.search.site import ReportDocument

# Markers ts_headline puts around highlighted words. Control characters that no
# ingestion path produces; summary_markup tolerates strays anyway.
HEADLINE_START = "\x02"
HEADLINE_STOP = "\x03"
_HIGHLIGHT = re.compile(f"{HEADLINE_START}([^{HEADLINE_START}{HEADLINE_STOP}]*){HEADLINE_STOP}")


class AnnotatedReportSearchIndex(ReportSearchIndex):
    """Typing shim for the annotations the hydration queryset adds to each row."""

    rank: float | None
    headline: str

    class Meta:
        abstract = True


def summary_markup(body: str, headline: str, max_words: int = 30) -> SafeString:
    """The excerpt shown on a result card, as HTML-safe markup.

    ``headline`` is the ts_headline output carrying the sentinel markers; when it
    is empty (no tsquery, or the document did not match it) the first ``max_words``
    words of the body are shown instead. The text is escaped first, balanced marker
    pairs then become <em>, and stray markers are dropped, so report text can never
    contribute markup.
    """
    text = headline if headline else " ".join(body.split()[:max_words])
    escaped = str(escape(text))
    highlighted = _HIGHLIGHT.sub(r"<em>\1</em>", escaped)
    cleaned = highlighted.replace(HEADLINE_START, "").replace(HEADLINE_STOP, "")
    return mark_safe(cleaned)


def document_from_pgsearch_response(
    record: ReportSearchIndex,
    *,
    relevance: float | None,
    summary: str,
    similarity: float | None,
    score: float,
) -> ReportDocument:
    report = record.report
    return ReportDocument(
        relevance=relevance,
        document_id=report.document_id,
        pacs_name=report.pacs_name,
        pacs_link=report.pacs_link,
        patient_age=report.patient_age,
        patient_sex=report.patient_sex,
        study_description=report.study_description,
        modalities=report.modality_codes,
        summary=summary,
        similarity=similarity,
        score=score,
    )
```

Delete `summary_with_fallback` from `radis/pgsearch/utils/fusion.py` (lines 27-33) and its three tests from `test_fusion.py` (lines 59-74; drop `summary_with_fallback` from the import on line 3).

- [ ] **Step 4: Wire the hydration loop to the new helper**

In `radis/pgsearch/providers.py`:
- import line 38 becomes `from .utils.fusion import rrf_fuse` and add `from .utils.document_utils import (AnnotatedReportSearchIndex, HEADLINE_START, HEADLINE_STOP, document_from_pgsearch_response, summary_markup)` (merge with the existing document_utils import on line 37);
- in `_headline` (lines 586-596) use `start_sel=HEADLINE_START, stop_sel=HEADLINE_STOP`;
- replace the hydration block (lines 619-646) with:

```python
    page_rows = (
        ReportSearchIndex.objects.filter(report_id__in=page_ids)
        .annotate(headline=summary_expr, rank=rank_expr)
        .select_related("report")
    )
    by_id = {r.report.pk: cast(AnnotatedReportSearchIndex, r) for r in page_rows}

    documents: list[ReportDocument] = []
    for rid in page_ids:
        row = by_id.get(rid)
        if row is None:
            continue
        dist = vec_distance.get(rid)
        documents.append(
            document_from_pgsearch_response(
                row,
                relevance=row.rank,
                summary=summary_markup(row.report.body, row.headline or ""),
                similarity=(1.0 - dist) if dist is not None else None,
                score=rrf_score_by_id.get(rid, 0.0),
            )
        )
```

The two `# type: ignore[attr-defined]` comments disappear with the assignments they covered; pyright would flag them as unnecessary.

In `radis/search/templates/search/_result_document.html` line 5 change `{{ document.summary|safe }}` to `{{ document.summary }}`.

In `radis/pgsearch/tests/test_language_config.py` lines 101-109 replace the helper name `summary_with_fallback` in the comment with `summary_markup`.

- [ ] **Step 5: Add the end-to-end escaping tests**

Append to `radis/pgsearch/tests/test_providers.py`:

```python
def test_summary_escapes_report_text_and_keeps_highlights():
    make_report("Findings: <b>pneumonia</b> & effusion.")

    result = providers.search(_search(parse("pneumonia"), language="en"))  # type: ignore[arg-type]

    summary = result.documents[0].summary
    assert "<em>pneumonia</em>" in summary
    assert "&lt;b&gt;" in summary
    assert "&amp;" in summary
    assert "<b>" not in summary
```

Append to `radis/search/tests/test_views.py`:

```python
@pytest.mark.django_db
def test_summaries_are_autoescaped_unless_the_provider_marks_them_safe(client: Client):
    from django.utils.safestring import mark_safe

    user = create_test_user_with_active_group()
    client.force_login(user)
    report = ReportFactory.create(document_id="ESC_1", language=LanguageFactory.create(code="en"))

    def document(summary):
        return ReportDocument(
            relevance=0.5,
            document_id=report.document_id,
            pacs_name=report.pacs_name,
            pacs_link=report.pacs_link,
            patient_age=report.patient_age,
            patient_sex=report.patient_sex,
            study_description=report.study_description,
            modalities=["CT"],
            summary=summary,
        )

    def mock_search(request):
        documents = [document("<b>plain</b>"), document(mark_safe("<em>safe</em>"))]
        return SearchResult(total_count=2, total_relation="exact", documents=documents)

    provider = SearchProvider(name="Escaping", search=mock_search, max_results=1000)
    with patch("radis.search.views.search_provider", provider):
        response = client.get("/search/", {"query": "chest"})

    content = response.content.decode()
    assert "&lt;b&gt;plain&lt;/b&gt;" in content
    assert "<em>safe</em>" in content
```

- [ ] **Step 6: Run the tests and the lint**

Run: `DJANGO_SETTINGS_MODULE=radis.settings.test uv run pytest -p no:cacheprovider radis/pgsearch radis/search -q`
Expected: all pass, including `test_language_config.py` (its `<em>effusion</em>` assertion proves the markers are restored end to end).

Run: `uv run cli lint`
Expected: clean (no unnecessary type-ignore reported in `providers.py`).

- [ ] **Step 7: Commit**

```bash
git add radis/pgsearch radis/search/templates/search/_result_document.html radis/search/tests/test_views.py
git commit -m "Escape the summary excerpt and highlight through sentinel markers" -m "ts_headline wraps highlights in control characters that cannot occur in reports, the provider escapes the text and restores <em> in Python, and the template no longer marks report text safe." -m "Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>" -m "Claude-Session: https://claude.ai/code/session_01XTntToXdCSFQYyhezJdYvc"
```

---

### Task 5: Full-text mode replaces fusion

Spec §4.1–§4.5. The provider ranks keyword hits only, re-orders the window when a description is given, caches in two layers, and never embeds for `count` and `retrieve`. The vector arm, RRF and the negation-exclusion helpers go, with the tests that pinned them. `search()` still takes a `FullTextSearch` in this task; Task 6 widens it.

**Files:**
- Modify: `radis/pgsearch/providers.py` (large: see steps)
- Modify: `radis/pgsearch/apps.py:171-174`, `radis/pgsearch/models.py:15-19`
- Modify: `radis/search/utils/query_parser.py:339-376` (rename), rename `radis/search/tests/test_query_parser_unparse_for_embedding.py` → `test_query_parser_unparse_positive_terms.py`
- Delete: `radis/pgsearch/utils/fusion.py`, `radis/pgsearch/tests/test_fusion.py`, `test_negative_query_string.py`, `test_provider_hybrid.py`, `test_fused_cache.py`
- Modify: `radis/pgsearch/tests/test_language_config.py:1-31,126-212`, `test_query_embedding_cache.py:1-127`, `test_embedding_failure_log_throttle.py:83`, `test_filter_query_equivalence.py:322-338`, `test_admin.py:471-476`, `test_search_settings.py` (append the GUC test)
- Test: `radis/pgsearch/tests/test_provider_fulltext.py` (new), `test_ranking_cache.py` (new), `radis/extractions/tests/test_tasks.py` (append)

**Interfaces:**
- Consumes: `rerank_window`, `min_max` (Task 3); `summary_markup`, `HEADLINE_START/STOP`, `document_from_pgsearch_response` (Task 4); the `SEARCH_*` settings (Task 2).
- Produces in `radis/pgsearch/providers.py`: `Ranking(ids, score_by_id, similarity_by_id, relation, configs, query_str, degraded)` NamedTuple; `_keyword_ranking(request: FullTextSearch) -> _KeywordRanking`; `_window_measurement_queryset(top_ids: list[int], vec: list[float])`; `_rank_fulltext(request: FullTextSearch, caller: str) -> Ranking`; `_hydration_queryset(page_ids, group, configs, query_str)`; `_hydrate(page_ids: list[int], ranking: Ranking, group: int | None) -> list[ReportDocument]`; `_page(ids, request) -> list[int]`; `search(request: FullTextSearch) -> SearchResult`; `count(request: FullTextSearch) -> int`; `retrieve(request: FullTextSearch) -> Iterator[str]`; `_cache_key(**parts) -> str`; `_embedding_fingerprint()`. `QueryParser.unparse_positive_terms(node) -> str`.

- [ ] **Step 1: Write the failing full-text provider tests**

Create `radis/pgsearch/tests/test_provider_fulltext.py`:

```python
import logging
from unittest.mock import patch

import pytest
from django.contrib.auth.models import Group
from django.db import connection
from django.test.utils import CaptureQueriesContext

from radis.core.utils.embedding_client import EmbeddingClientError
from radis.core.utils.model_spec import parse_model_spec
from radis.core.utils.rate_limit import RateLimited
from radis.pgsearch.models import ReportSearchIndex
from radis.pgsearch.providers import count, retrieve, search
from radis.reports.factories import ReportFactory
from radis.search.site import FullTextSearch, SearchFilters, SimilarityRerank
from radis.search.utils.query_parser import QueryParser

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def _embeddings_model_configured(settings):
    """Test settings default EMBEDDINGS_MODEL to None; the re-rank needs a model and a
    mocked client."""
    settings.EMBEDDINGS_MODEL = parse_model_spec("qwen3")


def _unit_vec(idx: int, dim: int) -> list[float]:
    v = [0.0] * dim
    v[idx % dim] = 1.0
    return v


def _request(query: str, group_id: int, description: str | None = None, **kwargs) -> FullTextSearch:
    node, _ = QueryParser().parse(query)
    assert node is not None
    return FullTextSearch(
        query=node,
        # language="en" so the query-side tsquery config matches the reports' config.
        filters=SearchFilters(group=group_id, language="en"),
        rerank=SimilarityRerank(description) if description else None,
        **kwargs,
    )


@pytest.fixture
def group(db):
    return Group.objects.create(name="radiology")


@pytest.fixture
def corpus(group, settings):
    """Three keyword hits and one report the keywords never match.

    ``twice`` mentions the keyword twice, so it has the highest ts_rank; ``near``
    and ``bare`` tie on ts_rank and fall back to id order (near was created first).
    """
    dim = settings.EMBEDDINGS_DIM
    twice = ReportFactory.create(
        body="No pneumothorax detected. Previous pneumothorax resolved.", language__code="en"
    )
    near = ReportFactory.create(body="Findings: pneumothorax on the left.", language__code="en")
    bare = ReportFactory.create(body="pneumothorax findings", language__code="en")
    other = ReportFactory.create(body="Lungs are clear bilaterally.", language__code="en")
    for report in (twice, near, bare, other):
        report.groups.add(group)
    ReportSearchIndex.objects.filter(report=twice).update(embedding=_unit_vec(99, dim))
    ReportSearchIndex.objects.filter(report=near).update(embedding=_unit_vec(0, dim))
    ReportSearchIndex.objects.filter(report=other).update(embedding=_unit_vec(0, dim))
    # ``bare`` keeps a NULL embedding: not embedded yet.
    return {"twice": twice, "near": near, "bare": bare, "other": other}


def _mock_client(dim: int, idx: int = 0):
    patcher = patch("radis.pgsearch.providers.EmbeddingClient")
    client_cls = patcher.start()
    client_cls.return_value.__enter__.return_value = client_cls.return_value
    client_cls.return_value.__exit__.return_value = None
    client_cls.return_value.embed_query.return_value = _unit_vec(idx, dim)
    return patcher, client_cls


@pytest.fixture
def embed(settings):
    patcher, client_cls = _mock_client(settings.EMBEDDINGS_DIM)
    yield client_cls
    patcher.stop()


def _ids(result) -> list[str]:
    return [d.document_id for d in result.documents]


def test_keyword_search_makes_no_embedding_call_and_keeps_keyword_order(group, corpus, embed):
    result = search(_request("pneumothorax", group.pk))

    assert embed.return_value.embed_query.call_count == 0
    assert _ids(result) == [corpus["twice"].document_id, corpus["near"].document_id, corpus["bare"].document_id]
    assert result.total_count == 3
    assert result.total_relation == "exact"
    assert result.degraded is False
    for doc in result.documents:
        assert doc.similarity is None
        assert doc.score == doc.relevance


def test_a_description_reranks_the_window(group, corpus, embed):
    result = search(_request("pneumothorax", group.pk, "free air in the pleural space"))

    # near is identical to the description vector (similarity 1), twice is orthogonal
    # (0); at w=0.7 near scores 0.7 and twice 0.3. bare has no vector and follows.
    assert _ids(result) == [corpus["near"].document_id, corpus["twice"].document_id, corpus["bare"].document_id]
    by_id = {d.document_id: d for d in result.documents}
    assert by_id[corpus["near"].document_id].similarity == pytest.approx(1.0)
    assert by_id[corpus["twice"].document_id].similarity == pytest.approx(0.0)
    assert by_id[corpus["bare"].document_id].similarity is None
    assert by_id[corpus["bare"].document_id].score == by_id[corpus["bare"].document_id].relevance
    assert corpus["other"].document_id not in by_id
    assert result.degraded is False


def test_the_rerank_embeds_the_description_not_the_keywords(group, corpus, embed):
    search(_request("pneumothorax", group.pk, "free air in the pleural space"))

    embed.return_value.embed_query.assert_called_once_with("free air in the pleural space")


def test_only_the_window_is_measured(group, corpus, embed, settings):
    settings.SEARCH_RERANK_WINDOW = 1
    result = search(_request("pneumothorax", group.pk, "free air"))

    # Only ``twice`` (the top keyword hit) is measured; the others keep keyword order.
    assert _ids(result) == [corpus["twice"].document_id, corpus["near"].document_id, corpus["bare"].document_id]
    assert result.documents[1].similarity is None


def test_the_measurement_is_one_id_lookup(group, corpus, embed):
    with CaptureQueriesContext(connection) as ctx:
        search(_request("pneumothorax", group.pk, "free air"))

    measurements = [q["sql"] for q in ctx.captured_queries if "<=>" in q["sql"]]
    assert len(measurements) == 1
    assert '"report_id" IN' in measurements[0]


@pytest.mark.parametrize(
    "failure",
    [EmbeddingClientError("down"), RateLimited()],
    ids=["client-error", "rate-limited"],
)
def test_embedding_failure_keeps_keyword_order_and_flags_degraded(group, corpus, embed, failure):
    embed.return_value.embed_query.side_effect = failure

    result = search(_request("pneumothorax", group.pk, "free air"))

    assert _ids(result) == [corpus["twice"].document_id, corpus["near"].document_id, corpus["bare"].document_id]
    assert result.degraded is True
    assert all(d.similarity is None for d in result.documents)


def test_openai_rate_limit_error_flags_degraded(group, corpus, embed):
    import httpx2
    import openai

    response = httpx2.Response(429, request=httpx2.Request("POST", "http://x"))
    embed.return_value.embed_query.side_effect = openai.RateLimitError(
        message="slow down", response=response, body=None
    )

    result = search(_request("pneumothorax", group.pk, "free air"))

    assert result.degraded is True
    assert result.total_count == 3


def test_a_rerank_without_a_model_embeds_nothing(group, corpus, settings, monkeypatch, caplog):
    from radis.pgsearch import providers

    settings.EMBEDDINGS_MODEL = None
    calls = []
    monkeypatch.setattr(providers, "_embed_query_cached", lambda text, caller: calls.append(text))

    with caplog.at_level(logging.ERROR, logger="radis.pgsearch.providers"):
        result = search(_request("pneumothorax", group.pk, "free air"))

    assert calls == []
    assert caplog.records == []
    assert result.degraded is False
    assert result.total_count == 3


def test_the_query_path_timeout_reaches_the_client(group, corpus, embed, settings):
    settings.EMBEDDINGS_QUERY_REQUEST_TIMEOUT_SECONDS = 3.5

    search(_request("pneumothorax", group.pk, "free air"))

    embed.assert_called_once_with(timeout=3.5)


def test_count_and_retrieve_ignore_the_rerank(group, corpus, embed):
    embed.return_value.embed_query.side_effect = AssertionError("must not embed")
    request = _request("pneumothorax", group.pk, "free air")

    assert count(request) == 3
    assert list(retrieve(request)) == [
        corpus["twice"].document_id, corpus["near"].document_id, corpus["bare"].document_id
    ]


def test_hydration_skips_a_report_removed_from_the_group(group, corpus, embed):
    request = _request("pneumothorax", group.pk)
    search(request)  # caches the keyword ranking
    corpus["near"].groups.remove(group)

    result = search(request)

    assert corpus["near"].document_id not in _ids(result)
    assert result.total_count == 3  # the cached count catches up when the entry expires


def test_no_report_traversal_in_the_provider_source():
    """Guarded in test_filter_query_equivalence as well; repeated here because the
    hydration query is new code on the hot path."""
    import inspect

    from radis.pgsearch import providers

    offending = [
        line.strip()
        for line in inspect.getsource(providers).splitlines()
        if "report__" in line
        and "report__body" not in line
        and "report__document_id" not in line
        and not line.strip().startswith("#")
    ]
    assert offending == []
```

- [ ] **Step 2: Write the failing ranking-cache tests**

Create `radis/pgsearch/tests/test_ranking_cache.py`:

```python
from unittest.mock import patch

import pytest
from django.contrib.auth.models import Group
from django.db import connection
from django.test.utils import CaptureQueriesContext

from radis.core.utils.embedding_client import EmbeddingClientError
from radis.core.utils.model_spec import parse_model_spec
from radis.pgsearch.models import ReportSearchIndex
from radis.pgsearch.providers import count, search
from radis.reports.factories import ReportFactory
from radis.search.site import FullTextSearch, SearchFilters, SimilarityRerank
from radis.search.utils.query_parser import QueryParser

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def _embeddings_model_configured(settings):
    settings.EMBEDDINGS_MODEL = parse_model_spec("qwen3")


def _unit_vec(idx: int, dim: int) -> list[float]:
    v = [0.0] * dim
    v[idx % dim] = 1.0
    return v


def _request(group_id: int, description: str | None = None, offset: int = 0, limit: int = 25):
    node, _ = QueryParser().parse("pneumothorax")
    assert node is not None
    return FullTextSearch(
        query=node,
        filters=SearchFilters(group=group_id, language="en"),
        rerank=SimilarityRerank(description) if description else None,
        offset=offset,
        limit=limit,
    )


@pytest.fixture
def group(db):
    return Group.objects.create(name="radiology")


@pytest.fixture
def reports(group, settings):
    dim = settings.EMBEDDINGS_DIM
    first = ReportFactory.create(body="Findings: pneumothorax on the left.", language__code="en")
    second = ReportFactory.create(body="No pneumothorax detected, lungs clear.", language__code="en")
    for report in (first, second):
        report.groups.add(group)
    ReportSearchIndex.objects.filter(report=first).update(embedding=_unit_vec(0, dim))
    ReportSearchIndex.objects.filter(report=second).update(embedding=_unit_vec(1, dim))
    return first, second


@pytest.fixture
def embed(settings):
    patcher = patch("radis.pgsearch.providers.EmbeddingClient")
    client_cls = patcher.start()
    client_cls.return_value.__enter__.return_value = client_cls.return_value
    client_cls.return_value.__exit__.return_value = None
    client_cls.return_value.embed_query.return_value = _unit_vec(0, settings.EMBEDDINGS_DIM)
    yield client_cls.return_value.embed_query
    patcher.stop()


def _ran_keyword_ranking(queries) -> bool:
    """The keyword layer ranks every match: ts_rank with no report_id list. The
    hydration query also annotates ts_rank, but only for an explicit page slice."""
    return any("ts_rank" in q["sql"] and '"report_id" IN' not in q["sql"] for q in queries)


def _ran_measurement(queries) -> bool:
    return any("<=>" in q["sql"] for q in queries)


def test_a_repeated_search_runs_no_ranking_query(group, reports):
    with CaptureQueriesContext(connection) as first:
        result1 = search(_request(group.pk))
    with CaptureQueriesContext(connection) as second:
        result2 = search(_request(group.pk))

    assert _ran_keyword_ranking(first.captured_queries)
    assert not _ran_keyword_ranking(second.captured_queries)
    assert [d.document_id for d in result1.documents] == [d.document_id for d in result2.documents]


def test_page_two_is_served_from_the_cached_ranking(group, reports):
    page1 = search(_request(group.pk, offset=0, limit=1))
    with CaptureQueriesContext(connection) as ctx:
        page2 = search(_request(group.pk, offset=1, limit=1))

    assert not _ran_keyword_ranking(ctx.captured_queries)
    assert len(page1.documents) == len(page2.documents) == 1
    assert page1.documents[0].document_id != page2.documents[0].document_id


def test_a_described_search_shares_the_keyword_layer_with_a_plain_one(group, reports, embed):
    search(_request(group.pk))
    with CaptureQueriesContext(connection) as ctx:
        search(_request(group.pk, "free air"))

    assert not _ran_keyword_ranking(ctx.captured_queries)
    assert _ran_measurement(ctx.captured_queries)


def test_a_repeated_described_search_runs_neither_layer(group, reports, embed):
    search(_request(group.pk, "free air"))
    with CaptureQueriesContext(connection) as ctx:
        result = search(_request(group.pk, "free air"))

    assert not _ran_keyword_ranking(ctx.captured_queries)
    assert not _ran_measurement(ctx.captured_queries)
    assert embed.call_count == 1
    assert result.documents[0].similarity == pytest.approx(1.0)


def test_the_extraction_count_shares_the_keyword_layer(group, reports):
    search(_request(group.pk))
    with CaptureQueriesContext(connection) as ctx:
        assert count(_request(group.pk)) == 2

    assert not _ran_keyword_ranking(ctx.captured_queries)


def test_filters_participate_in_the_key(reports, settings):
    other = Group.objects.create(name="cardiology")
    r_other = ReportFactory.create(body="No pneumothorax after drainage.", language__code="en")
    r_other.groups.add(other)
    first, _ = reports

    result_first = search(_request(first.groups.first().pk))
    result_other = search(_request(other.pk))

    assert first.document_id in {d.document_id for d in result_first.documents}
    assert {d.document_id for d in result_other.documents} == {r_other.document_id}


def test_the_description_and_the_weight_participate_in_the_window_key(group, reports, embed, settings):
    search(_request(group.pk, "free air"))
    with CaptureQueriesContext(connection) as other_text:
        search(_request(group.pk, "collapsed lung"))
    settings.SEARCH_RERANK_SEMANTIC_WEIGHT = 0.5
    with CaptureQueriesContext(connection) as other_weight:
        search(_request(group.pk, "collapsed lung"))

    assert _ran_measurement(other_text.captured_queries)
    assert _ran_measurement(other_weight.captured_queries)


def test_a_degraded_window_is_not_cached_but_the_keyword_layer_is(group, reports, embed):
    embed.side_effect = EmbeddingClientError("down")
    degraded = search(_request(group.pk, "free air"))
    assert degraded.degraded is True

    embed.side_effect = None
    with CaptureQueriesContext(connection) as ctx:
        recovered = search(_request(group.pk, "free air"))

    assert not _ran_keyword_ranking(ctx.captured_queries)
    assert _ran_measurement(ctx.captured_queries)
    assert recovered.degraded is False
    assert recovered.documents[0].similarity == pytest.approx(1.0)


def test_zero_timeout_disables_the_cache(group, reports, settings):
    settings.SEARCH_RANKING_CACHE_TIMEOUT_SECONDS = 0
    search(_request(group.pk))
    with CaptureQueriesContext(connection) as ctx:
        search(_request(group.pk))

    assert _ran_keyword_ranking(ctx.captured_queries)
```

- [ ] **Step 3: Run both new modules to verify they fail**

Run: `DJANGO_SETTINGS_MODULE=radis.settings.test uv run pytest -p no:cacheprovider radis/pgsearch/tests/test_provider_fulltext.py radis/pgsearch/tests/test_ranking_cache.py -q`
Expected: failures (`FullTextSearch` has no `rerank` effect yet: the described searches return vector-only neighbours, `degraded` is always False, `count` embeds).

- [ ] **Step 4: Delete the fusion machinery from `providers.py`**

Remove, in this order, and fix imports as you go:
- `_collect_and_negations`, `_build_negative_query_string`, `_exclude_negations` (lines 137-188);
- `_FusedHybrid`, `_FUSED_CACHE_SCHEMA`, `_fused_cache_key`, `_fused_to_cached`, `_fused_from_cached`, `_fuse_hybrid`, `_fuse_hybrid_uncached` (lines 395-564);
- the bodies of `search`, `count`, `retrieve` (replaced in Step 6);
- the `RRF_K` constant and `from .utils.fusion import rrf_fuse`; then delete `radis/pgsearch/utils/fusion.py` and `radis/pgsearch/tests/test_fusion.py` (`git rm`).

Replace the group predicate in `_build_filter_query` (lines 207-210) with a helper so hydration can reuse it:

```python
def _group_query(group: int | None) -> Q:
    """Group-scoped access control. ``None`` is fail-closed: an empty ``group_ids``
    matches only reports assigned to no group at all."""
    if group is None:
        return Q(group_ids=[])
    return Q(group_ids__contains=[group])
```

and in `_build_filter_query`: `fq = _group_query(filters.group)` (keep the surrounding comment about the single-table projection).

Reword `_embed_query_or_none` (lines 269-307): the exception call becomes
`logger.exception("%s unavailable; embedding service looks misconfigured", caller)`; the throttled warning becomes

```python
            logger.warning(
                "%s unavailable; embedding service still misconfigured for this "
                "configuration (check EMBEDDINGS_BASE_URL=%r and EMBEDDINGS_MODEL=%r): "
                "%s: %s -- the search runs degraded until this is fixed; the full "
                "traceback was already logged once for this configuration",
                caller, base_url, model, type(exc).__name__, exc,
            )
```

and the transient branch `logger.warning("%s unavailable: %s", caller, e)`. Its docstring: "Embed the text, or return None to signal a degraded search." In `_embed_query_cached` delete the history comment ("Task 3 adds the caller-side guard … in the meantime") and reword the docstring sentence to "Failures are not cached: a transient outage must not pin searches to a degraded result for the TTL."

- [ ] **Step 5: Add the keyword layer, the window layer and the ranking record**

Below `_fts_candidate_queryset` add:

```python
class _KeywordRanking(NamedTuple):
    """The keyword layer: every hit of the tsquery in ts_rank order."""

    ids: list[int]
    ts_rank_by_id: dict[int, float]
    relation: Literal["exact", "at_least", "approximately"]
    configs: list[tuple[str, list[str]]]
    query_str: str


class Ranking(NamedTuple):
    """One mode's ordered result list, with what the page needs to explain it."""

    ids: list[int]
    score_by_id: dict[int, float]
    similarity_by_id: dict[int, float]
    relation: Literal["exact", "at_least", "approximately"]
    configs: list[tuple[str, list[str]]]
    query_str: str
    degraded: bool


# Bumped whenever a cached payload's shape changes, so entries written by an older
# code version are missed rather than misread.
_RANKING_CACHE_SCHEMA = 2


def _embedding_fingerprint() -> list[object] | None:
    spec = settings.EMBEDDINGS_MODEL
    if spec is None:
        return None
    return [
        settings.EMBEDDINGS_BASE_URL,
        spec.model,
        spec.params,
        settings.EMBEDDINGS_QUERY_INSTRUCTION,
        settings.EMBEDDINGS_DIM,
    ]


def _cache_key(**parts: object) -> str:
    """A shared cache backend (production uses the database) outlives process restarts
    and configuration changes, so everything that determines a ranking is part of the
    key, hashed so a 1,000-character description cannot overflow it."""
    fingerprint = json.dumps(
        {"schema": _RANKING_CACHE_SCHEMA, **parts}, sort_keys=True, default=str
    )
    return "pgsearch-ranking-" + hashlib.sha256(fingerprint.encode()).hexdigest()


def _keyword_ranking(request: FullTextSearch) -> _KeywordRanking:
    """The keyword layer, cached whenever the cache is on. It never depends on the
    embedding service, so a plain search, a described search and an extraction over
    the same query and filters share one entry."""
    timeout = settings.SEARCH_RANKING_CACHE_TIMEOUT_SECONDS
    key = _cache_key(
        layer="keyword",
        query=QueryParser.unparse(request.query),
        filters=asdict(request.filters),
        fts_max=settings.SEARCH_FTS_MAX_RESULTS,
    )
    if timeout > 0:
        cached = cache.get(key)
        if cached is not None:
            return _KeywordRanking(
                ids=list(cached["ids"]),
                ts_rank_by_id=dict(zip(cached["ids"], cached["ranks"], strict=True)),
                relation=cached["relation"],
                configs=[(config, list(codes)) for config, codes in cached["configs"]],
                query_str=cached["query_str"],
            )
    ranking = _keyword_ranking_uncached(request)
    if timeout > 0:
        cache.set(
            key,
            {
                "ids": ranking.ids,
                "ranks": [ranking.ts_rank_by_id[rid] for rid in ranking.ids],
                "relation": ranking.relation,
                "configs": ranking.configs,
                "query_str": ranking.query_str,
            },
            timeout=timeout,
        )
    return ranking


def _keyword_ranking_uncached(request: FullTextSearch) -> _KeywordRanking:
    query_str = _build_query_string(request.query)
    configs = _language_configs(request.filters)
    filter_query = _build_filter_query(request.filters)
    # An empty ``configs`` means an empty corpus (Report.language is a required FK),
    # and an empty match would match every row rather than none, so skip it.
    rows = [] if not configs else list(_fts_candidate_queryset(filter_query, configs, query_str))
    ids = [row["report_id"] for row in rows]
    relation: Literal["exact", "at_least", "approximately"] = (
        "at_least" if len(rows) >= settings.SEARCH_FTS_MAX_RESULTS else "exact"
    )
    return _KeywordRanking(
        ids=ids,
        ts_rank_by_id={row["report_id"]: float(row["rank"]) for row in rows},
        relation=relation,
        configs=configs,
        query_str=query_str,
    )


def _window_measurement_queryset(top_ids: list[int], vec: list[float]):
    """Exact cosine distance of the window hits: a lookup on the unique report_id
    column (never ``pk``, index-row ids differ from report ids), served by its B-tree
    index rather than the HNSW index, so it stays off the candidate query."""
    return (
        ReportSearchIndex.objects.filter(report_id__in=top_ids)
        .exclude(embedding__isnull=True)
        .annotate(distance=CosineDistance("embedding", vec))
        .values_list("report_id", "distance")
    )


def _window_order(
    request: FullTextSearch, keyword: _KeywordRanking, caller: str
) -> tuple[list[tuple[int, float]] | None, dict[int, float], bool]:
    """The window layer of a described search.

    Returns the ``(report_id, score)`` pairs of the whole list in result order, the
    similarity per measured id, and whether the embedding failed. ``None`` pairs mean
    no re-rank applies (no description, no embedding model, or degraded). The cached
    payload is the similarities; the order is recomputed from the keyword layer."""
    rerank = request.rerank
    if rerank is None or settings.EMBEDDINGS_MODEL is None:
        return None, {}, False
    window = settings.SEARCH_RERANK_WINDOW
    weight = settings.SEARCH_RERANK_SEMANTIC_WEIGHT
    timeout = settings.SEARCH_RANKING_CACHE_TIMEOUT_SECONDS
    key = _cache_key(
        layer="window",
        query=QueryParser.unparse(request.query),
        filters=asdict(request.filters),
        fts_max=settings.SEARCH_FTS_MAX_RESULTS,
        text=rerank.text,
        window=window,
        weight=weight,
        embeddings=_embedding_fingerprint(),
    )
    hits = [(rid, keyword.ts_rank_by_id[rid]) for rid in keyword.ids]
    if timeout > 0:
        cached = cache.get(key)
        if cached is not None:
            similarity = dict(zip(cached["ids"], cached["similarities"], strict=True))
            return rerank_window(hits, similarity, window, weight), similarity, False
    vec = _embed_query_cached(rerank.text, caller)
    if vec is None:
        return None, {}, True
    similarity = {
        rid: 1.0 - float(dist)
        for rid, dist in _window_measurement_queryset(keyword.ids[:window], vec)
    }
    if timeout > 0:
        cache.set(
            key,
            {"ids": list(similarity), "similarities": list(similarity.values())},
            timeout=timeout,
        )
    return rerank_window(hits, similarity, window, weight), similarity, False


def _rank_fulltext(request: FullTextSearch, caller: str) -> Ranking:
    keyword = _keyword_ranking(request)
    pairs, similarity, degraded = _window_order(request, keyword, caller)
    if pairs is None:
        return Ranking(
            ids=list(keyword.ids),
            score_by_id=dict(keyword.ts_rank_by_id),
            similarity_by_id={},
            relation=keyword.relation,
            configs=keyword.configs,
            query_str=keyword.query_str,
            degraded=degraded,
        )
    return Ranking(
        ids=[rid for rid, _ in pairs],
        score_by_id=dict(pairs),
        similarity_by_id=similarity,
        relation=keyword.relation,
        configs=keyword.configs,
        query_str=keyword.query_str,
        degraded=False,
    )
```

Add `from .utils.ranking import rerank_window` to the imports; `asdict`, `hashlib`, `json`, `NamedTuple`, `Literal` and `cast` stay imported.

- [ ] **Step 6: Replace hydration, `search`, `count` and `retrieve`**

```python
def _hydration_queryset(
    page_ids: list[int],
    group: int | None,
    configs: list[tuple[str, list[str]]],
    query_str: str,
):
    """The page slice: single-table apart from the headline's deliberate join on
    ``report__body``. The group predicate keeps a report removed from the group off
    the page at once even while the cached ranking still lists it."""
    rows = ReportSearchIndex.objects.filter(report_id__in=page_ids).filter(_group_query(group))
    if not configs or not query_str:
        return rows.annotate(
            headline=Value("", output_field=TextField()),
            rank=Value(None, output_field=FloatField()),
        )
    tsqueries = {
        config: SearchQuery(query_str, search_type="raw", config=config) for config, _ in configs
    }

    def _headline(config: str) -> SearchHeadline:
        return SearchHeadline(
            "report__body",
            tsqueries[config],
            config=config,
            start_sel=HEADLINE_START,
            stop_sel=HEADLINE_STOP,
            min_words=10,
            max_words=20,
            max_fragments=10,
        )

    # Highlight and rank each document under the configuration it was indexed under;
    # a headline or rank built under another configuration silently yields nothing.
    headline_expr = Case(
        *[When(language_code__in=codes, then=_headline(config)) for config, codes in configs],
        default=Value(""),
        output_field=TextField(),
    )
    rank_expr = Case(
        *[
            When(language_code__in=codes, then=SearchRank(F("search_vector"), tsqueries[config]))
            for config, codes in configs
        ],
        default=Value(0.0),
        output_field=FloatField(),
    )
    return rows.annotate(headline=headline_expr, rank=rank_expr)


def _hydrate(page_ids: list[int], ranking: Ranking, group: int | None) -> list[ReportDocument]:
    rows = _hydration_queryset(page_ids, group, ranking.configs, ranking.query_str)
    by_id = {r.report.pk: cast(AnnotatedReportSearchIndex, r) for r in rows.select_related("report")}
    documents: list[ReportDocument] = []
    for rid in page_ids:
        row = by_id.get(rid)
        if row is None:  # deleted, or removed from the group, since the ranking was cached
            continue
        documents.append(
            document_from_pgsearch_response(
                row,
                relevance=row.rank,
                summary=summary_markup(row.report.body, row.headline or ""),
                similarity=ranking.similarity_by_id.get(rid),
                score=ranking.score_by_id.get(rid, 0.0),
            )
        )
    return documents


def _page(ids: list[int], request: Search) -> list[int]:
    if request.limit is None:
        return ids[request.offset :]
    return ids[request.offset : request.offset + request.limit]


def search(request: FullTextSearch) -> SearchResult:
    ranking = _rank_fulltext(request, "Full-text re-rank")
    documents = _hydrate(_page(ranking.ids, request), ranking, request.filters.group)
    return SearchResult(
        total_count=len(ranking.ids),
        total_relation=ranking.relation,
        documents=documents,
        degraded=ranking.degraded,
    )


def count(request: FullTextSearch) -> int:
    # A count does not change under a re-order, so the re-rank is ignored and the
    # embedding service is never called on this path.
    return len(_keyword_ranking(request).ids)


def retrieve(request: FullTextSearch) -> Iterator[str]:
    # An extraction set has no order; the re-rank is ignored (see ``count``).
    ordered_ids = _keyword_ranking(request).ids
    if not ordered_ids:
        return iter([])
    id_to_doc = dict(Report.objects.filter(pk__in=ordered_ids).values_list("pk", "document_id"))
    return (id_to_doc[rid] for rid in ordered_ids if rid in id_to_doc)
```

Import `Search` alongside `FullTextSearch` from `radis.search.site`. Replace the `apps.py` comment (lines 171-174) with `# The paging cap of a full-text list; a semantic list is at most the beam width long.` and the `models.py` docstring phrase "for hybrid search" with "for semantic search and the full-text re-rank".

- [ ] **Step 7: Rename the parser helper**

In `radis/search/utils/query_parser.py` rename `unparse_for_embedding` to `unparse_positive_terms` (both the definition and the three recursive calls) with this docstring:

```python
        """Render the query as a plain bag of its positive terms.

        Drops every ``NOT`` branch, the ``AND``/``OR`` tokens, grouping parentheses
        and phrase quotes, leaving the words a description of the wanted reports
        should be about. Returns the empty string when the whole query reduces to
        ``NOT`` clauses. Used by the search page's description writer.
        """
```

`git mv radis/search/tests/test_query_parser_unparse_for_embedding.py radis/search/tests/test_query_parser_unparse_positive_terms.py` and rename the test function and the call inside it to `unparse_positive_terms`.

- [ ] **Step 8: Retire the fusion tests and fix the surviving modules**

- `git rm radis/pgsearch/tests/test_provider_hybrid.py radis/pgsearch/tests/test_fused_cache.py radis/pgsearch/tests/test_negative_query_string.py`.
- `test_language_config.py`: delete `test_negation_excludes_leaking_vector_candidates_under_each_documents_own_config` (lines 167-212), the sentence "This does NOT exercise `_exclude_negations` … with two configs." from the docstring at lines 131-135, the parenthetical "(not `_exclude_negations`, the separate vector-side path this test never reaches)" at lines 158-161, and the now-unused imports (`patch`, `parse_model_spec`, `ReportSearchIndex`) and `_unit_vec` helper.
- `test_query_embedding_cache.py`: rewrite lines 1-127 so the four database tests use the re-rank. `_make_search(description, group_id, offset=0)` returns `FullTextSearch(query=<"pneumothorax">, filters=SearchFilters(group=group_id, language="en"), rerank=SimilarityRerank(description), offset=offset, limit=25)`; the fixture becomes `report` (body "Findings: pneumothorax on the left.", language "en", in the group, embedding `_unit_vec(0, dim)`); the tests: `test_paginated_search_embeds_the_description_only_once` (two searches, `embed_query.call_count == 1`, both results carry `similarity == pytest.approx(1.0)` for the report); `test_embedding_failure_is_not_cached` (side effects error then vector; first `degraded is True`, second `degraded is False` with the similarity); `test_different_descriptions_embed_separately` (two texts, two calls); delete `test_retrieve_shares_cache_with_search` (retrieve ignores the re-rank). Reword the autouse fixture docstring to "(no embedding model, full-text only)" and the `_make_search` comment to "see test_provider_fulltext.py". The two fingerprint tests at the end stay as they are.
- `test_embedding_failure_log_throttle.py:83`: `assert "degraded" in message`.
- `test_filter_query_equivalence.py:322-338`: the docstring's first line becomes "The FTS candidate queryset ``_keyword_ranking`` actually runs."
- `test_admin.py:471-476`: replace `test_provider_hybrid.py` with `test_provider_fulltext.py`.
- Move the GUC test into `test_search_settings.py` (it needs the `db` fixture and `from django.db import connection`):

```python
def test_connection_carries_the_beam_width(db):
    """hnsw.ef_search rides along as a libpq connection option, so every connection
    scans the index at the configured depth; iterative scan is off on a plain
    connection (semantic searches switch it on for their own transaction)."""
    with connection.cursor() as cursor:
        # pgvector defines the hnsw.* GUCs only once its library is loaded.
        cursor.execute("SELECT '[1]'::vector(1)")
        cursor.execute("SHOW hnsw.ef_search")
        assert cursor.fetchone()[0] == str(settings.SEARCH_HNSW_EF_SEARCH)
        cursor.execute("SHOW hnsw.iterative_scan")
        assert cursor.fetchone()[0] == "off"
```

- Append to `radis/extractions/tests/test_tasks.py` (next to the real-provider test that imports `pgsearch_count`/`pgsearch_retrieve`):

```python
@pytest.mark.django_db
def test_extraction_retrieval_never_calls_the_embedding_service(monkeypatch, settings):
    from unittest.mock import patch

    from radis.core.utils.model_spec import parse_model_spec
    from radis.search.site import FullTextSearch, SearchFilters, SimilarityRerank
    from radis.search.utils.query_parser import QueryParser

    settings.EMBEDDINGS_MODEL = parse_model_spec("qwen3")
    group = GroupFactory.create()
    report = ReportFactory.create(body="Befund: Pneumothorax links.", language=LanguageFactory.create(code="de"))
    report.groups.add(group)
    node, _ = QueryParser().parse("Pneumothorax")
    assert node is not None
    request = FullTextSearch(
        query=node,
        filters=SearchFilters(group=group.pk, language="de"),
        rerank=SimilarityRerank("free air"),
        offset=0,
        limit=0,
    )

    with patch("radis.pgsearch.providers.EmbeddingClient") as client_cls:
        client_cls.return_value.__enter__.side_effect = AssertionError("must not embed")
        assert pgsearch_count(request) == 1
        assert list(pgsearch_retrieve(request)) == [report.document_id]
```

(Import `GroupFactory` from `adit_radis_shared.accounts.factories` if the module does not already.)

- [ ] **Step 9: Run the suites and the lint**

Run: `DJANGO_SETTINGS_MODULE=radis.settings.test uv run pytest -p no:cacheprovider radis/pgsearch radis/search radis/extractions -q`
Expected: all pass. `test_providers.py`'s full-text contract is unchanged; `test_language_config.py` still proves per-configuration matching and NOT.

Run: `uv run cli lint`
Expected: clean. ruff F401 will name any import left over from the deleted code; pyright will name any stale `type: ignore`.

Run: `git grep -n "rrf\|_exclude_negations\|unparse_for_embedding\|summary_with_fallback" -- radis`
Expected: no output.

- [ ] **Step 10: Commit**

```bash
git add -A radis
git commit -m "Rank full-text searches by keywords only, re-ranking the window by a description" -m "Fusion, the vector arm and the negation-exclusion helpers go. A full-text search returns exactly the keyword hits; a description re-orders the top SEARCH_RERANK_WINDOW hits by a blend of similarity and keyword rank, hits without a vector follow in keyword order, and an embedding failure flags the result degraded. The ranking cache keeps the keyword layer and the window layer apart, so an outage never re-runs the keyword ranking, and count and retrieve read the keyword layer only." -m "Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>" -m "Claude-Session: https://claude.ai/code/session_01XTntToXdCSFQYyhezJdYvc"
```

---

### Task 6: Semantic mode in the provider

Spec §5. `search()` takes either request type; a `SemanticSearch` runs the HNSW scan under the filters with the per-search iterative scan, is cached as its own entry, and is truncated by the minimum similarity after retrieval. The registration declares the capability.

**Files:**
- Modify: `radis/pgsearch/providers.py` (imports; add `ITERATIVE_SCAN_SQL`, `_semantic_scan_queryset`, `_rank_semantic`, `_semantic_ranking`, `_effective_min_similarity`, `_truncate`; widen `search`)
- Modify: `radis/pgsearch/apps.py:168-177`
- Modify: `radis/pgsearch/tests/test_filter_query_equivalence.py` (append), `test_query_embedding_cache.py` (append), `test_search_settings.py` (append)
- Test: `radis/pgsearch/tests/test_provider_semantic.py` (new)

**Interfaces:**
- Consumes: `Ranking`, `_cache_key`, `_embedding_fingerprint`, `_embed_query_cached`, `_build_filter_query`, `_hydrate`, `_page` (Task 5).
- Produces: `ITERATIVE_SCAN_SQL = "SET LOCAL hnsw.iterative_scan = strict_order"`; `_semantic_scan_queryset(filter_query: Q, vec: list[float])`; `_rank_semantic(request: SemanticSearch, caller: str) -> Ranking`; `_effective_min_similarity(request: SemanticSearch) -> float | None`; `_truncate(ranking: Ranking, cutoff: float | None) -> Ranking`; `search(request: SearchRequest) -> SearchResult`; the registered `SearchProvider.semantic` flag.

- [ ] **Step 1: Write the failing semantic provider tests**

Create `radis/pgsearch/tests/test_provider_semantic.py`:

```python
from unittest.mock import patch

import pytest
from django.contrib.auth.models import Group
from django.db import connection
from django.test.utils import CaptureQueriesContext

from radis.core.utils.embedding_client import EmbeddingClientError
from radis.core.utils.model_spec import parse_model_spec
from radis.pgsearch.models import ReportSearchIndex
from radis.pgsearch.providers import ITERATIVE_SCAN_SQL, search
from radis.reports.factories import ReportFactory
from radis.search.site import SearchFilters, SemanticSearch

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def _embeddings_model_configured(settings):
    settings.EMBEDDINGS_MODEL = parse_model_spec("qwen3")


def _vec(dim: int, components: dict[int, float]) -> list[float]:
    v = [0.0] * dim
    for idx, value in components.items():
        v[idx] = value
    return v


def _request(group_id: int, text: str = "free air in the pleural space", **kwargs) -> SemanticSearch:
    return SemanticSearch(text=text, filters=SearchFilters(group=group_id, language="en"), **kwargs)


@pytest.fixture
def group(db):
    return Group.objects.create(name="radiology")


@pytest.fixture
def corpus(group, settings):
    """Similarities to the mocked query vector (1, 0, ...): near 0.8, mid 0.6, far 0.0;
    ``unembedded`` has no vector and is invisible in semantic mode."""
    dim = settings.EMBEDDINGS_DIM
    near = ReportFactory.create(body="Lungs are clear bilaterally & no effusion.", language__code="en")
    mid = ReportFactory.create(body="Findings: pneumothorax on the left.", language__code="en")
    far = ReportFactory.create(body="Degenerative changes of the spine.", language__code="en")
    unembedded = ReportFactory.create(body="Not embedded yet.", language__code="en")
    for report in (near, mid, far, unembedded):
        report.groups.add(group)
    ReportSearchIndex.objects.filter(report=near).update(embedding=_vec(dim, {0: 0.8, 1: 0.6}))
    ReportSearchIndex.objects.filter(report=mid).update(embedding=_vec(dim, {0: 0.6, 1: 0.8}))
    ReportSearchIndex.objects.filter(report=far).update(embedding=_vec(dim, {1: 1.0}))
    return {"near": near, "mid": mid, "far": far, "unembedded": unembedded}


@pytest.fixture
def embed(settings):
    patcher = patch("radis.pgsearch.providers.EmbeddingClient")
    client_cls = patcher.start()
    client_cls.return_value.__enter__.return_value = client_cls.return_value
    client_cls.return_value.__exit__.return_value = None
    client_cls.return_value.embed_query.return_value = _vec(settings.EMBEDDINGS_DIM, {0: 1.0})
    yield client_cls.return_value.embed_query
    patcher.stop()


def _ids(result) -> list[str]:
    return [d.document_id for d in result.documents]


def test_results_come_in_similarity_order(group, corpus, embed):
    result = search(_request(group.pk))

    assert _ids(result) == [corpus["near"].document_id, corpus["mid"].document_id, corpus["far"].document_id]
    assert result.total_count == 3
    assert result.total_relation == "exact"
    assert result.degraded is False
    near_doc = result.documents[0]
    assert near_doc.similarity == pytest.approx(0.8)
    assert near_doc.score == near_doc.similarity
    assert near_doc.relevance is None
    assert near_doc.summary == "Lungs are clear bilaterally &amp; no effusion."
    assert "<em>" not in near_doc.summary
    embed.assert_called_once_with("free air in the pleural space")


def test_filters_apply(corpus, embed, settings):
    other = Group.objects.create(name="cardiology")
    foreign = ReportFactory.create(body="Cardiomegaly.", language__code="en")
    foreign.groups.add(other)
    ReportSearchIndex.objects.filter(report=foreign).update(
        embedding=_vec(settings.EMBEDDINGS_DIM, {0: 1.0})
    )

    result = search(_request(other.pk))

    assert _ids(result) == [foreign.document_id]


def test_the_setting_truncates_the_list(group, corpus, embed, settings):
    settings.SEARCH_SEMANTIC_MIN_SIMILARITY = 0.5

    result = search(_request(group.pk))

    assert _ids(result) == [corpus["near"].document_id, corpus["mid"].document_id]
    assert result.total_count == 2


def test_the_per_search_cutoff_beats_the_setting(group, corpus, embed, settings):
    settings.SEARCH_SEMANTIC_MIN_SIMILARITY = 0.5

    assert _ids(search(_request(group.pk, min_similarity=0.7))) == [corpus["near"].document_id]
    assert len(_ids(search(_request(group.pk, min_similarity=0.0)))) == 3


def test_a_cutoff_nothing_passes_gives_an_empty_result(group, corpus, embed):
    result = search(_request(group.pk, min_similarity=1.0))

    assert result.documents == []
    assert result.total_count == 0
    assert result.degraded is False


def test_truncation_happens_after_retrieval(group, corpus, embed):
    search(_request(group.pk, min_similarity=0.7))
    with CaptureQueriesContext(connection) as ctx:
        result = search(_request(group.pk))

    assert not any("<=>" in q["sql"] for q in ctx.captured_queries)
    assert result.total_count == 3


def test_iterative_scan_is_set_for_the_scan_only(group, corpus, embed, settings):
    settings.SEARCH_SEMANTIC_ITERATIVE_SCAN = True
    with CaptureQueriesContext(connection) as ctx:
        search(_request(group.pk))

    statements = [q["sql"] for q in ctx.captured_queries]
    scan_index = next(i for i, sql in enumerate(statements) if "<=>" in sql)
    assert statements[scan_index - 1] == ITERATIVE_SCAN_SQL


def test_iterative_scan_can_be_switched_off(group, corpus, embed, settings):
    settings.SEARCH_SEMANTIC_ITERATIVE_SCAN = False
    with CaptureQueriesContext(connection) as ctx:
        search(_request(group.pk))

    assert not any("hnsw.iterative_scan" in q["sql"] for q in ctx.captured_queries)


def test_the_list_is_capped_at_the_beam_width(group, corpus, embed, settings):
    settings.SEARCH_HNSW_EF_SEARCH = 2
    with CaptureQueriesContext(connection) as ctx:
        result = search(_request(group.pk))

    assert result.total_count == 2
    assert any("LIMIT 2" in q["sql"] and "<=>" in q["sql"] for q in ctx.captured_queries)


def test_a_repeated_search_is_served_from_the_cache(group, corpus, embed):
    search(_request(group.pk))
    with CaptureQueriesContext(connection) as ctx:
        result = search(_request(group.pk))

    assert not any("<=>" in q["sql"] for q in ctx.captured_queries)
    assert embed.call_count == 1
    assert result.total_count == 3


def test_embedding_failure_is_empty_and_degraded_and_not_cached(group, corpus, embed, settings):
    embed.side_effect = [EmbeddingClientError("down"), _vec(settings.EMBEDDINGS_DIM, {0: 1.0})]

    first = search(_request(group.pk))
    second = search(_request(group.pk))

    assert first.documents == [] and first.total_count == 0 and first.degraded is True
    assert second.degraded is False and second.total_count == 3
    assert embed.call_count == 2


def test_a_semantic_search_without_a_model_is_a_caller_error(group, corpus, settings):
    settings.EMBEDDINGS_MODEL = None

    with pytest.raises(ValueError):
        search(_request(group.pk))
```

- [ ] **Step 2: Add the SQL-shape, cache-key and capability tests**

Append to `radis/pgsearch/tests/test_filter_query_equivalence.py`:

```python
def test_semantic_scan_is_single_table_and_uses_the_hnsw_index(corpus):
    """The semantic scan must touch no table but the search index and must use the
    HNSW index. On a three-row table the planner prefers a sequential scan, so the
    EXPLAIN runs with sequential scans disabled for its transaction."""
    from django.db import connection, transaction

    from radis.pgsearch.providers import _semantic_scan_queryset

    filters = SearchFilters(group=corpus["group_a"].pk, language="en")
    vec = [0.0] * 1024
    vec[0] = 1.0
    queryset = _semantic_scan_queryset(_build_filter_query(filters), vec)
    sql, params = queryset.query.sql_with_params()

    assert "DISTINCT" not in sql
    with transaction.atomic(), connection.cursor() as cursor:
        cursor.execute("SET LOCAL enable_seqscan = off")
        cursor.execute(f"EXPLAIN {sql}", params)
        plan = "\n".join(row[0] for row in cursor.fetchall())

    assert "pgsearch_embedding_hnsw" in plan, plan
    for table in ("reports_report", "reports_language", "reports_modality"):
        assert table not in plan, plan


def test_window_measurement_is_an_id_lookup(corpus):
    from django.db import connection

    from radis.pgsearch.providers import _window_measurement_queryset

    vec = [0.0] * 1024
    vec[0] = 1.0
    queryset = _window_measurement_queryset([1, 2, 3], vec)
    sql, params = queryset.query.sql_with_params()

    assert '"report_id" IN' in sql
    with connection.cursor() as cursor:
        cursor.execute(f"EXPLAIN {sql}", params)
        plan = "\n".join(row[0] for row in cursor.fetchall())
    assert "pgsearch_embedding_hnsw" not in plan, plan
    for table in ("reports_report", "reports_language", "reports_modality"):
        assert table not in plan, plan


def test_hydration_query_joins_nothing_but_the_report_body(corpus):
    """The headline deliberately joins reports_report for the body; nothing else."""
    from django.db import connection

    from radis.pgsearch.providers import _hydration_queryset

    filters = SearchFilters(group=corpus["group_a"].pk, language="en")
    node, _ = QueryParser().parse("pneumonia")
    assert node is not None
    queryset = _hydration_queryset(
        [1, 2], filters.group, _language_configs(filters), _build_query_string(node)
    )
    sql, params = queryset.query.sql_with_params()

    assert "group_ids" in sql
    with connection.cursor() as cursor:
        cursor.execute(f"EXPLAIN {sql}", params)
        plan = "\n".join(row[0] for row in cursor.fetchall())
    for table in ("reports_language", "reports_modality"):
        assert table not in plan, plan
```

(`corpus`, `SearchFilters`, `QueryParser`, `_build_filter_query`, `_language_configs`, `_build_query_string` already exist in that module.)

Append to `radis/pgsearch/tests/test_query_embedding_cache.py`:

```python
def test_a_repeated_semantic_search_embeds_once(group, report, settings):
    from radis.search.site import SemanticSearch

    patcher, embed_query = _mock_embedding_client(settings.EMBEDDINGS_DIM)
    request = SemanticSearch(text="free air", filters=SearchFilters(group=group.pk, language="en"))
    try:
        first = search(request)
        second = search(request)
    finally:
        patcher.stop()

    assert embed_query.call_count == 1
    assert [d.document_id for d in first.documents] == [report.document_id]
    assert [d.document_id for d in second.documents] == [report.document_id]


def test_a_failed_semantic_embedding_is_not_cached(group, report, settings):
    from radis.search.site import SemanticSearch

    patcher, embed_query = _mock_embedding_client(settings.EMBEDDINGS_DIM)
    embed_query.side_effect = [EmbeddingClientError("down"), _unit_vec(0, settings.EMBEDDINGS_DIM)]
    request = SemanticSearch(text="free air", filters=SearchFilters(group=group.pk, language="en"))
    try:
        first = search(request)
        second = search(request)
    finally:
        patcher.stop()

    assert first.degraded is True and first.documents == []
    assert second.degraded is False and len(second.documents) == 1
```

Append to `radis/pgsearch/tests/test_search_settings.py`:

```python
def test_the_registered_provider_reports_the_semantic_capability(settings):
    from radis.search.site import search_provider

    assert search_provider is not None
    assert search_provider.semantic is (settings.EMBEDDINGS_MODEL is not None)
```

- [ ] **Step 3: Run the new tests to verify they fail**

Run: `DJANGO_SETTINGS_MODULE=radis.settings.test uv run pytest -p no:cacheprovider radis/pgsearch/tests/test_provider_semantic.py radis/pgsearch/tests/test_filter_query_equivalence.py radis/pgsearch/tests/test_query_embedding_cache.py radis/pgsearch/tests/test_search_settings.py -q`
Expected: `ImportError: cannot import name 'ITERATIVE_SCAN_SQL'` and `AttributeError` for the new provider helpers.

- [ ] **Step 4: Add the semantic ranking to `providers.py`**

Imports: `from django.db import connection, transaction`; `from radis.search.site import FullTextSearch, ReportDocument, Search, SearchFilters, SearchRequest, SearchResult, SemanticSearch`. Then add below `_rank_fulltext`:

```python
ITERATIVE_SCAN_SQL = "SET LOCAL hnsw.iterative_scan = strict_order"


def _semantic_scan_queryset(filter_query: Q, vec: list[float]):
    """The nearest reports under the filters: one HNSW index scan, capped at the beam
    width, which is the semantic depth. The LIMIT must stay in the query: without one
    the planner abandons the index for a sequential sort."""
    return (
        ReportSearchIndex.objects.filter(filter_query)
        .exclude(embedding__isnull=True)
        .annotate(distance=CosineDistance("embedding", vec))
        .order_by("distance", "report_id")
        .values_list("report_id", "distance")[: settings.SEARCH_HNSW_EF_SEARCH]
    )


def _semantic_ranking(ids: list[int], similarity: dict[int, float], *, degraded: bool) -> Ranking:
    return Ranking(
        ids=ids,
        score_by_id=dict(similarity),
        similarity_by_id=similarity,
        relation="exact",
        configs=[],
        query_str="",
        degraded=degraded,
    )


def _rank_semantic(request: SemanticSearch, caller: str) -> Ranking:
    """The reports most similar to the text, in similarity order; cached unless the
    embedding failed, in which case the list is empty and degraded."""
    if settings.EMBEDDINGS_MODEL is None:
        raise ValueError("A semantic search needs an embedding model (EMBEDDINGS_MODEL).")
    timeout = settings.SEARCH_RANKING_CACHE_TIMEOUT_SECONDS
    key = _cache_key(
        layer="semantic",
        text=request.text,
        filters=asdict(request.filters),
        ef_search=settings.SEARCH_HNSW_EF_SEARCH,
        iterative=settings.SEARCH_SEMANTIC_ITERATIVE_SCAN,
        embeddings=_embedding_fingerprint(),
    )
    if timeout > 0:
        cached = cache.get(key)
        if cached is not None:
            similarity = dict(zip(cached["ids"], cached["similarities"], strict=True))
            return _semantic_ranking(list(cached["ids"]), similarity, degraded=False)
    vec = _embed_query_cached(request.text, caller)
    if vec is None:
        return _semantic_ranking([], {}, degraded=True)
    filter_query = _build_filter_query(request.filters)
    # SET LOCAL lasts to the end of the enclosing transaction, so the scan gets its own:
    # the search view runs outside any transaction and the setting ends with the scan.
    with transaction.atomic():
        if settings.SEARCH_SEMANTIC_ITERATIVE_SCAN:
            with connection.cursor() as cursor:
                cursor.execute(ITERATIVE_SCAN_SQL)
        rows = list(_semantic_scan_queryset(filter_query, vec))
    ids = [rid for rid, _ in rows]
    similarity = {rid: 1.0 - float(dist) for rid, dist in rows}
    if timeout > 0:
        cache.set(
            key,
            {"ids": ids, "similarities": [similarity[rid] for rid in ids]},
            timeout=timeout,
        )
    return _semantic_ranking(ids, similarity, degraded=False)


def _effective_min_similarity(request: SemanticSearch) -> float | None:
    if request.min_similarity is not None:
        return request.min_similarity
    return settings.SEARCH_SEMANTIC_MIN_SIMILARITY


def _truncate(ranking: Ranking, cutoff: float | None) -> Ranking:
    """Cut the similarity-ordered list at the first hit below ``cutoff``. Done after
    retrieval rather than as a SQL predicate: with iterative scan on, pgvector would
    keep scanning for rows that can no longer pass, up to its tuple budget."""
    if cutoff is None or cutoff <= 0:
        return ranking
    kept: list[int] = []
    for rid in ranking.ids:
        if ranking.similarity_by_id.get(rid, 0.0) < cutoff:
            break
        kept.append(rid)
    return ranking._replace(ids=kept)
```

Replace `search`:

```python
def search(request: SearchRequest) -> SearchResult:
    if isinstance(request, SemanticSearch):
        ranking = _truncate(
            _rank_semantic(request, "Semantic search"), _effective_min_similarity(request)
        )
    else:
        ranking = _rank_fulltext(request, "Full-text re-rank")
    documents = _hydrate(_page(ranking.ids, request), ranking, request.filters.group)
    return SearchResult(
        total_count=len(ranking.ids),
        total_relation=ranking.relation,
        documents=documents,
        degraded=ranking.degraded,
    )
```

In `radis/pgsearch/apps.py` register `SearchProvider(name="PG Search", search=search, max_results=settings.SEARCH_FTS_MAX_RESULTS, semantic=settings.EMBEDDINGS_MODEL is not None)`.

- [ ] **Step 5: Run the pgsearch suite and the lint**

Run: `DJANGO_SETTINGS_MODULE=radis.settings.test uv run pytest -p no:cacheprovider radis/pgsearch radis/search radis/extractions -q`
Expected: all pass.

Run: `uv run cli lint`
Expected: clean.

- [ ] **Step 6: Commit**

```bash
git add radis/pgsearch
git commit -m "Add semantic mode to the search provider" -m "A SemanticSearch lists the reports nearest to its text under the filters, capped at the beam width, with hnsw.iterative_scan set for the scan's own transaction so selective filters are topped up. The minimum similarity truncates the cached list after retrieval, an embedding failure yields an empty degraded result, and the registration declares whether semantic search is available." -m "Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>" -m "Claude-Session: https://claude.ai/code/session_01XTntToXdCSFQYyhezJdYvc"
```

---

### Task 7: The search form

Spec §6.1, §6.2 (field definitions). The form grows the mode select, the description textarea and the cutoff field when the provider has the capability, a server-side cap on the keyword query, the `to_filters` helper, and the small ORM-free form the description endpoint binds.

**Files:**
- Modify: `radis/search/forms.py` (whole file)
- Modify: `radis/search/layouts.py` (whole file), `radis/search/templates/search/form_elements/query_input.html` (whole file)
- Create: `radis/search/templates/search/form_elements/description_input.html`
- Test: `radis/search/tests/test_forms.py` (append)

**Interfaces:**
- Produces: `SearchForm(data=None, *, semantic: bool = False)` with fields `query` (max 200), `mode` (choices `("", "Full-text")`, `("semantic", "Semantic")`), `description` (max 1000, whitespace-collapsed), `min_similarity` (0..1), the three latter deleted when `semantic` is `False`; `SearchForm.to_filters(group_id: int) -> SearchFilters`; `SearchForm.semantic_mode() -> bool`; `DescriptionRequestForm` with `query` (max 200) and `description` (max 1000); `QueryInput(query_field, mode_field=None, description_field=None, query_attrs=None)` rendering `query_input.html` with `query_field`, `mode_field`, `description_field` in its context; the partial `description_input.html` taking `value`, `notice`.

- [ ] **Step 1: Write the failing form tests**

Append to `radis/search/tests/test_forms.py`:

```python
def _query_layout_names(form: SearchForm) -> list[str]:
    from radis.search.layouts import QueryInput

    names: list[str] = []
    for item in form.query_helper.layout.fields:
        if isinstance(item, QueryInput):
            names.append(item.query_field)
            names.extend(f for f in (item.mode_field, item.description_field) if f)
    return names


@pytest.mark.django_db
def test_semantic_fields_exist_only_with_the_capability() -> None:
    with_capability = SearchForm(semantic=True)
    without = SearchForm()
    for name in ("mode", "description", "min_similarity"):
        assert name in with_capability.fields
        assert name not in without.fields
    assert _query_layout_names(with_capability) == ["query", "mode", "description"]
    assert _query_layout_names(without) == ["query"]


@pytest.mark.django_db
def test_mode_choices_default_to_full_text() -> None:
    form = SearchForm(semantic=True)
    assert form.fields["mode"].choices == [("", "Full-text"), ("semantic", "Semantic")]  # type: ignore[attr-defined]
    bound = SearchForm({"query": "pneumothorax"}, semantic=True)
    assert bound.is_valid()
    assert bound.semantic_mode() is False
    assert SearchForm({"description": "free air", "mode": "semantic"}, semantic=True).is_valid()


@pytest.mark.django_db
def test_description_whitespace_is_collapsed() -> None:
    form = SearchForm({"query": "x", "description": "  pleural \n air  "}, semantic=True)
    assert form.is_valid()
    assert form.cleaned_data["description"] == "pleural air"


@pytest.mark.django_db
def test_description_over_the_limit_is_invalid() -> None:
    form = SearchForm({"query": "x", "description": "a" * 1001}, semantic=True)
    assert not form.is_valid()
    assert "description" in form.errors


@pytest.mark.django_db
def test_query_over_200_characters_is_invalid() -> None:
    form = SearchForm({"query": "a" * 201})
    assert not form.is_valid()
    assert "query" in form.errors


@pytest.mark.django_db
@pytest.mark.parametrize("value", ["1.5", "-0.1"])
def test_min_similarity_outside_unit_interval_is_invalid(value) -> None:
    form = SearchForm({"mode": "semantic", "description": "x", "min_similarity": value}, semantic=True)
    assert not form.is_valid()
    assert "min_similarity" in form.errors


@pytest.mark.django_db
def test_min_similarity_is_optional_and_shows_the_default_as_placeholder(settings) -> None:
    settings.SEARCH_SEMANTIC_MIN_SIMILARITY = 0.3
    form = SearchForm({"mode": "semantic", "description": "x"}, semantic=True)
    assert form.is_valid()
    assert form.cleaned_data["min_similarity"] is None
    assert form.fields["min_similarity"].widget.attrs["placeholder"] == "default 0.30"
    assert form.fields["min_similarity"].initial is None


@pytest.mark.django_db
def test_to_filters_carries_every_filter_field() -> None:
    LanguageFactory.create(code="en")
    form = SearchForm(
        {"query": "x", "language": "en", "study_description": "CT", "patient_sex": "F",
         "age_from": 20, "age_till": 60},
        semantic=True,
    )
    assert form.is_valid(), form.errors
    filters = form.to_filters(7)
    assert filters.group == 7
    assert filters.language == "en"
    assert filters.study_description == "CT"
    assert filters.patient_sex == "F"
    assert (filters.patient_age_from, filters.patient_age_till) == (20, 60)
    assert filters.labels == []


def test_description_request_form_limits_match_the_search_form() -> None:
    from radis.search.forms import DescriptionRequestForm

    assert DescriptionRequestForm({"query": "a" * 200, "description": "b" * 1000}).is_valid()
    assert not DescriptionRequestForm({"query": "a" * 201}).is_valid()
    assert not DescriptionRequestForm({"description": "b" * 1001}).is_valid()
```

- [ ] **Step 2: Run them to verify they fail**

Run: `DJANGO_SETTINGS_MODULE=radis.settings.test uv run pytest -p no:cacheprovider radis/search/tests/test_forms.py -v`
Expected: `TypeError: SearchForm.__init__() got an unexpected keyword argument 'semantic'` and `ImportError` for `DescriptionRequestForm`.

- [ ] **Step 3: Rewrite `radis/search/forms.py`**

```python
from typing import Any

from crispy_forms.helper import FormHelper, Layout
from crispy_forms.layout import Button, Div, Field
from django import forms
from django.conf import settings

from radis.core.constants import AGE_STEP
from radis.core.form_fields import (
    create_age_range_fields,
    create_language_field,
    create_modality_field,
)
from radis.core.layouts import RangeSlider
from radis.labels.models import Label

from .layouts import QueryInput
from .site import SearchFilters

QUERY_MAX_LENGTH = 200
DESCRIPTION_MAX_LENGTH = 1000
MODE_SEMANTIC = "semantic"
MODE_CHOICES = [("", "Full-text"), (MODE_SEMANTIC, "Semantic")]


def _description_field() -> forms.CharField:
    return forms.CharField(
        required=False,
        label=False,  # type: ignore
        max_length=DESCRIPTION_MAX_LENGTH,
        widget=forms.Textarea(attrs={"rows": 2}),
    )


class SearchForm(forms.Form):
    # Query fields
    query = forms.CharField(required=False, label=False, max_length=QUERY_MAX_LENGTH)  # type: ignore
    # The empty value is full-text, so the default never appears in the URL.
    mode = forms.ChoiceField(required=False, choices=MODE_CHOICES, label=False)  # type: ignore
    description = _description_field()
    # Semantic mode only: overrides SEARCH_SEMANTIC_MIN_SIMILARITY for one search.
    min_similarity = forms.FloatField(
        required=False,
        min_value=0.0,
        max_value=1.0,
        label="Minimum similarity",
        widget=forms.NumberInput(attrs={"step": "0.01"}),
    )
    # Filter fields - language, modalities, labels, and age fields created in __init__
    study_date_from = forms.DateField(
        required=False, widget=forms.DateInput(attrs={"type": "date"})
    )
    study_date_till = forms.DateField(
        required=False, widget=forms.DateInput(attrs={"type": "date"})
    )
    study_description = forms.CharField(required=False)
    patient_sex = forms.ChoiceField(
        required=False, choices=[("", "All"), ("M", "Male"), ("F", "Female")]
    )

    def __init__(self, *args, semantic: bool = False, **kwargs):
        """``semantic`` is the registered provider's capability: without it the
        mode select, the description and the cutoff do not exist on the page."""
        super().__init__(*args, **kwargs)

        if not semantic:
            for name in ("mode", "description", "min_similarity"):
                del self.fields[name]
        else:
            default_cutoff = settings.SEARCH_SEMANTIC_MIN_SIMILARITY
            if default_cutoff is not None:
                self.fields["min_similarity"].widget.attrs["placeholder"] = f"default {default_cutoff:.2f}"

        # Create fields using factory functions (use codes, not PKs)
        self.fields["language"] = create_language_field(use_pk=False, empty_label="All")
        self.fields["modalities"] = create_modality_field(use_pk=False)
        age_from, age_till = create_age_range_fields()
        self.fields["age_from"] = age_from
        self.fields["age_till"] = age_till

        active_labels = Label.objects.filter(active=True).order_by("name")
        self.fields["labels"] = forms.MultipleChoiceField(
            required=False, choices=[(label.name, label.name) for label in active_labels]
        )
        self.fields["labels"].widget.attrs["size"] = 6

        self.query_helper = FormHelper()
        self.query_helper.template = "search/form_elements/form_part.html"  # type: ignore
        self.query_helper.form_tag = False
        self.query_helper.disable_csrf = True
        self.query_helper.layout = self.create_query_layout()

        self.filters_helper = FormHelper()
        self.filters_helper.template = "search/form_elements/form_part.html"  # type: ignore
        self.filters_helper.form_tag = False
        self.filters_helper.disable_csrf = True
        self.filters_helper.layout = self.create_filters_layout()

    def create_query_layout(self) -> Layout:
        semantic = "mode" in self.fields
        return Layout(
            QueryInput(
                "query",
                mode_field="mode" if semantic else None,
                description_field="description" if semantic else None,
                query_attrs={
                    "placeholder": "Input your search terms",
                    "autocomplete": "off",
                    "maxlength": QUERY_MAX_LENGTH,
                },
            ),
        )

    def create_filters_layout(self) -> Layout:
        label_field = (
            [Field("labels", css_class="form-select-sm")]
            if self.fields["labels"].choices  # type: ignore
            else []
        )
        cutoff_field = (
            [
                Div(
                    Field(
                        "min_similarity",
                        css_class="form-control-sm",
                        **{":disabled": "mode !== 'semantic'"},
                    ),
                    **{"x-show": "mode === 'semantic'"},
                )
            ]
            if "min_similarity" in self.fields
            else []
        )
        return Layout(
            Field("language", css_class="form-select-sm"),
            Field("modalities", css_class="form-select-sm"),
            *label_field,
            Field("study_date_from", css_class="form-control-sm"),
            Field("study_date_till", css_class="form-control-sm"),
            Field("study_description", css_class="form-control-sm"),
            Field("patient_sex", css_class="form-select-sm"),
            RangeSlider("Age range", "age_from", "age_till", group_class="input-group-sm"),
            *cutoff_field,
            Div(
                Button(
                    "reset_filters",
                    "Reset filters",
                    type="button",
                    css_class="btn btn-sm btn-secondary",
                    **{"@click": "resetFilters"},
                ),
                css_class="d-flex justify-content-center",
            ),
        )

    def semantic_mode(self) -> bool:
        return self.cleaned_data.get("mode") == MODE_SEMANTIC

    def to_filters(self, group_id: int) -> SearchFilters:
        """The structured filters of a validated form, scoped to ``group_id``."""
        data = self.cleaned_data
        return SearchFilters(
            group=group_id,
            language=data["language"],
            modalities=data["modalities"],
            study_date_from=data["study_date_from"],
            study_date_till=data["study_date_till"],
            study_description=data["study_description"],
            patient_sex=data["patient_sex"],
            patient_age_from=data["age_from"],
            patient_age_till=data["age_till"],
            labels=data["labels"],
        )

    def clean_description(self) -> str:
        return " ".join(self.cleaned_data["description"].split())

    def clean_age_from(self) -> int | None:
        age_from = self.cleaned_data["age_from"]
        if age_from is not None and age_from % AGE_STEP != 0:
            raise forms.ValidationError(f"Age from must be a multiple of {AGE_STEP}")
        return age_from

    def clean_age_till(self) -> int | None:
        age_till = self.cleaned_data["age_till"]
        if age_till is not None and age_till % AGE_STEP != 0:
            raise forms.ValidationError(f"Age till must be a multiple of {AGE_STEP}")
        return age_till

    def clean(self) -> dict[str, Any] | None:
        age_from = self.cleaned_data["age_from"]
        age_till = self.cleaned_data["age_till"]

        if age_from is not None and age_till is not None and age_from >= age_till:
            raise forms.ValidationError("Age from must be less than age till")

        return super().clean()


class DescriptionRequestForm(forms.Form):
    """What the "Write from keywords" endpoint accepts: the same limits as the
    search form, with no database-backed fields, so an async view can bind it."""

    query = forms.CharField(required=False, max_length=QUERY_MAX_LENGTH)
    description = forms.CharField(required=False, max_length=DESCRIPTION_MAX_LENGTH)
```

- [ ] **Step 4: Widen the query layout object and its templates**

Rewrite `radis/search/layouts.py`:

```python
from typing import Any

from crispy_forms.layout import TemplateNameMixin
from django.forms import Form
from django.template.loader import render_to_string
from django.utils.html import conditional_escape


class QueryInput(TemplateNameMixin):
    """The search row: the mode select (when given), the keyword input, and the
    description row (when given), rendered by ``form_elements/query_input.html``."""

    def __init__(
        self,
        query_field: str,
        *,
        mode_field: str | None = None,
        description_field: str | None = None,
        css_class: str | None = None,
        query_attrs: dict[str, Any] | None = None,
        **kwargs,
    ) -> None:
        self.query_field = query_field
        self.mode_field = mode_field
        self.description_field = description_field

        self.query_attrs: dict[str, Any] = {}

        if css_class:
            self.query_attrs["class"] = css_class

        if query_attrs:
            self.query_attrs.update(query_attrs)

        self.query_attrs.update(
            {k.replace("_", "-"): conditional_escape(v) for k, v in kwargs.items()}
        )

    def render(self, form: Form, context: dict[str, Any], *args, **kwargs) -> str:
        query_field = form[self.query_field]

        if self.query_attrs:
            query_field.field.widget.attrs.update(self.query_attrs)

        description_field = form[self.description_field] if self.description_field else None
        return render_to_string(
            "search/form_elements/query_input.html",
            {
                "query_field": query_field,
                "mode_field": form[self.mode_field] if self.mode_field else None,
                "description_field": description_field,
                "description_value": description_field.value() or "" if description_field else "",
            },
        )
```

Rewrite `radis/search/templates/search/form_elements/query_input.html`:

```html
{% load crispy_field from crispy_forms_field %}
{% load bootstrap_icon from common_extras %}
<div x-data="QueryInput()">
    <div class="input-group">
        {% if mode_field %}
            {% crispy_field mode_field "class" "form-select flex-grow-0 w-auto" "x-model.fill" "mode" "aria-label" "Search mode" %}
        {% else %}
            <span class="input-group-text">Search</span>
        {% endif %}
        {% crispy_field query_field "class" "form-control" ":readonly" "mode === 'semantic'" %}
        <button type="button"
                class="btn bg-transparent"
                @click="clearQuery()"
                style="width: 40px;
                       margin-left: -40px;
                       padding: 0;
                       z-index: 100">{% bootstrap_icon "x" 20 %}</button>
        <button type="submit" class="btn btn-primary">{% bootstrap_icon "search" 20 %}</button>
    </div>
    {% if description_field %}
        {% include "search/form_elements/description_input.html" with value=description_value notice="" %}
    {% endif %}
</div>
```

Create `radis/search/templates/search/form_elements/description_input.html` (form-free: the endpoint renders it too, so it must not need a form instance):

```html
{% load bootstrap_icon from common_extras %}
<div class="input-group mt-2" id="description_row">
    <textarea name="description"
              id="id_description"
              class="form-control form-control-sm"
              rows="2"
              maxlength="1000"
              :placeholder="mode === 'semantic' ? 'Describe the reports you are looking for' : 'Optional: describe the reports you are looking for; the top hits are re-ranked by it'"
              @keydown.enter.exact.prevent="$el.form.requestSubmit()">{{ value }}</textarea>
    <button type="button"
            class="btn btn-outline-secondary btn-sm"
            x-show="mode !== 'semantic'"
            hx-post="{% url 'search_description' %}"
            hx-target="#id_description"
            hx-swap="outerHTML"
            hx-disabled-elt="this"
            hx-sync="this:drop"
            hx-indicator="#description_indicator">
        Write from keywords
        <span id="description_indicator" class="htmx-indicator spinner-border spinner-border-sm" role="status"></span>
    </button>
    <small id="description_notice" class="w-100 text-danger" hx-swap-oob="true">{{ notice }}</small>
</div>
```

The `{% url 'search_description' %}` resolves once Task 10 adds the route. Until then, register a placeholder route now in `radis/search/urls.py` so templates render: `path("description/", SearchView.as_view(), name="search_description")` is wrong (it would answer GETs with the page); instead add in Task 7 a minimal view returning 405 for anything, replaced in Task 10:

```python
# radis/search/views.py (temporary, replaced in Task 10)
from django.http import HttpResponseNotAllowed

def description_view(request):
    return HttpResponseNotAllowed(["POST"])
```

and `path("description/", description_view, name="search_description")` in `urls.py`.

- [ ] **Step 5: Run the form tests, then the whole search app**

Run: `DJANGO_SETTINGS_MODULE=radis.settings.test uv run pytest -p no:cacheprovider radis/search -q`
Expected: the form tests pass; `test_views.py` and `test_views_e2e.py` still pass (the view does not pass `semantic` yet, so the page has no mode select, which is today's page).

Run: `uv run cli lint`
Expected: clean (djlint checks the two templates).

- [ ] **Step 6: Commit**

```bash
git add radis/search
git commit -m "Give the search form a mode, a description and a similarity cutoff" -m "The three fields exist only when the provider has an embedding model. The keyword query gets a server-side limit, the filters build moves into to_filters, and a small ORM-free form carries the same limits for the description endpoint." -m "Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>" -m "Claude-Session: https://claude.ai/code/session_01XTntToXdCSFQYyhezJdYvc"
```

---

### Task 8: The search page: mode dispatch and per-mode rendering

Spec §6.3, §6.4, §6.6. The view builds the right request type, the templates word the count line, the notices, the result header and the info card per mode, and the page's Alpine component carries the mode state.

**Files:**
- Modify: `radis/search/views.py` (`SearchView.get`)
- Modify: `radis/search/templates/search/search.html`, `_search_results.html`, `_result_header.html`, `_search_info.html`
- Modify: `radis/search/static/search/search.js:1-15`
- Test: `radis/search/tests/test_views.py` (append; extend `test_search_view_returns_200_when_embedding_model_unset`)

**Interfaces:**
- Consumes: `SearchForm(data, semantic=...)`, `semantic_mode()`, `to_filters()` (Task 7); `FullTextSearch`, `SemanticSearch`, `SimilarityRerank`, `SearchResult.degraded` (Task 1).
- Produces: template context keys `mode` (`"fulltext"` or `"semantic"`), `description`, `reranked`, `window`, `weight`, `weight_complement`, `cutoff`, `degraded`, `mode_notice`, `page_sizes`, plus today's `offset`, `total_count`, `total_relation`, `fixed_query`, `paginator`, `page_obj`, `documents`; `PAGE_SIZES = [10, 25, 50]`.

- [ ] **Step 1: Write the failing view tests**

Append to `radis/search/tests/test_views.py`:

```python
from radis.search.site import FullTextSearch, SemanticSearch  # add to the imports at the top


def _document(report, *, relevance=0.5, similarity=None, score=0.0, summary="Summary"):
    return ReportDocument(
        relevance=relevance,
        document_id=report.document_id,
        pacs_name=report.pacs_name,
        pacs_link=report.pacs_link,
        patient_age=report.patient_age,
        patient_sex=report.patient_sex,
        study_description=report.study_description,
        modalities=["CT"],
        summary=summary,
    )._replace(similarity=similarity, score=score)


def _text(response) -> str:
    """The rendered page with whitespace collapsed, so phrases split across template
    lines can be asserted as one string."""
    return " ".join(response.content.decode().split())


def _provider(result: SearchResult, *, semantic: bool = True, captured: dict | None = None):
    def search(request):
        if captured is not None:
            captured["request"] = request
        return result

    return SearchProvider(name="Stub", search=search, max_results=1000, semantic=semantic)


def _empty(degraded: bool = False) -> SearchResult:
    return SearchResult(total_count=0, total_relation="exact", documents=[], degraded=degraded)


@pytest.fixture
def two_reports(db):
    language = LanguageFactory.create(code="en")
    return [
        ReportFactory.create(document_id=f"MODE_{i}", language=language, pacs_link="http://pacs")
        for i in (1, 2)
    ]


@pytest.mark.django_db
def test_mode_fields_exist_only_with_the_capability(client: Client):
    client.force_login(create_test_user_with_active_group())
    with patch("radis.search.views.search_provider", _provider(_empty(), semantic=True)):
        with_fields = client.get("/search/").context["form"].fields
    with patch("radis.search.views.search_provider", _provider(_empty(), semantic=False)):
        without = client.get("/search/").context["form"].fields
    assert "mode" in with_fields and "description" in with_fields and "min_similarity" in with_fields
    assert "mode" not in without and "description" not in without


@pytest.mark.django_db
def test_semantic_mode_builds_a_semantic_search_and_ignores_the_keywords(client: Client):
    client.force_login(create_test_user_with_active_group())
    captured: dict = {}
    with patch("radis.search.views.search_provider", _provider(_empty(), captured=captured)):
        response = client.get(
            "/search/",
            {"mode": "semantic", "description": "free  air", "query": "ignored", "min_similarity": "0.4"},
        )
    assert response.status_code == 200
    request = captured["request"]
    assert isinstance(request, SemanticSearch)
    assert request.text == "free air"
    assert request.min_similarity == 0.4
    assert response.context["mode"] == "semantic"


@pytest.mark.django_db
def test_full_text_with_a_description_builds_a_rerank(client: Client):
    client.force_login(create_test_user_with_active_group())
    captured: dict = {}
    with patch("radis.search.views.search_provider", _provider(_empty(), captured=captured)):
        client.get("/search/", {"query": "chest", "description": "free air"})
    request = captured["request"]
    assert isinstance(request, FullTextSearch)
    assert request.rerank is not None and request.rerank.text == "free air"


@pytest.mark.django_db
def test_full_text_without_a_description_has_no_rerank(client: Client):
    client.force_login(create_test_user_with_active_group())
    captured: dict = {}
    with patch("radis.search.views.search_provider", _provider(_empty(), captured=captured)):
        client.get("/search/", {"query": "chest"})
    assert captured["request"].rerank is None


@pytest.mark.django_db
def test_semantic_mode_without_a_description_shows_the_notice(client: Client):
    client.force_login(create_test_user_with_active_group())
    captured: dict = {}
    with patch("radis.search.views.search_provider", _provider(_empty(), captured=captured)):
        response = client.get("/search/", {"mode": "semantic", "query": "chest"})
    assert "request" not in captured
    assert response.context["mode_notice"] == "Semantic mode needs a description."
    assert "documents" not in response.context


@pytest.mark.django_db
def test_full_text_with_a_description_but_no_keywords_shows_the_notice(client: Client):
    client.force_login(create_test_user_with_active_group())
    captured: dict = {}
    with patch("radis.search.views.search_provider", _provider(_empty(), captured=captured)):
        response = client.get("/search/", {"description": "free air"})
    assert "request" not in captured
    assert response.context["mode_notice"] == (
        "Full-text mode needs keywords. Switch to Semantic to search by description alone."
    )


@pytest.mark.django_db
def test_semantic_url_without_the_capability_runs_full_text(client: Client):
    client.force_login(create_test_user_with_active_group())
    captured: dict = {}
    provider = _provider(_empty(), semantic=False, captured=captured)
    with patch("radis.search.views.search_provider", provider):
        response = client.get("/search/", {"mode": "semantic", "description": "x", "query": "chest"})
    assert response.status_code == 200
    assert isinstance(captured["request"], FullTextSearch)
    assert captured["request"].rerank is None


@pytest.mark.django_db
def test_page_beyond_a_short_semantic_list_is_clamped(client: Client, two_reports):
    client.force_login(create_test_user_with_active_group())
    result = SearchResult(
        total_count=2, total_relation="exact", documents=[], degraded=False
    )
    with patch("radis.search.views.search_provider", _provider(result)):
        response = client.get("/search/", {"mode": "semantic", "description": "x", "page": "3"})
    assert response.status_code == 200
    assert response.context["page_obj"].number == 1


@pytest.mark.django_db
def test_count_line_below_the_window(client: Client, two_reports):
    client.force_login(create_test_user_with_active_group())
    documents = [_document(r, similarity=0.9, score=0.8) for r in two_reports]
    result = SearchResult(total_count=2, total_relation="exact", documents=documents)
    with patch("radis.search.views.search_provider", _provider(result)):
        response = client.get("/search/", {"query": "chest", "description": "free air"})
    content = _text(response)
    assert "2 reports found, re-ranked by your description" in content
    assert "Re-rank score:" in content
    assert "Similarity:" in content


@pytest.mark.django_db
def test_count_line_above_the_window(client: Client, two_reports, settings):
    settings.SEARCH_RERANK_WINDOW = 1
    client.force_login(create_test_user_with_active_group())
    documents = [_document(two_reports[0], similarity=0.9, score=0.8), _document(two_reports[1])]
    result = SearchResult(total_count=2, total_relation="exact", documents=documents)
    with patch("radis.search.views.search_provider", _provider(result)):
        response = client.get("/search/", {"query": "chest", "description": "free air"})
    content = _text(response)
    assert "2 reports found, the top 1 re-ranked by your description, the rest in keyword order" in content
    assert content.count("Re-rank score:") == 1


@pytest.mark.django_db
def test_count_line_when_capped(client: Client):
    client.force_login(create_test_user_with_active_group())
    result = SearchResult(total_count=10000, total_relation="at_least", documents=[])
    with patch("radis.search.views.search_provider", _provider(result)):
        response = client.get("/search/", {"query": "chest"})
    assert "More than 10000 reports found" in _text(response)


@pytest.mark.django_db
def test_semantic_count_line_names_the_cutoff(client: Client, two_reports, settings):
    settings.SEARCH_SEMANTIC_MIN_SIMILARITY = 0.3
    client.force_login(create_test_user_with_active_group())
    documents = [_document(r, relevance=None, similarity=0.7, score=0.7) for r in two_reports]
    result = SearchResult(total_count=2, total_relation="exact", documents=documents)
    with patch("radis.search.views.search_provider", _provider(result)):
        response = client.get("/search/", {"mode": "semantic", "description": "free air"})
    content = _text(response)
    assert "The 2 reports most similar to your description with similarity at least 0.30" in content
    assert "Full-text rank:" not in content
    assert "Re-rank score:" not in content
    assert "Similarity:" in content


@pytest.mark.django_db
def test_semantic_zero_hits(client: Client):
    client.force_login(create_test_user_with_active_group())
    with patch("radis.search.views.search_provider", _provider(_empty())):
        response = client.get("/search/", {"mode": "semantic", "description": "free air"})
    content = _text(response)
    assert "No reports similar to your description" in content
    assert "No results found" not in content


@pytest.mark.django_db
def test_degraded_full_text_shows_the_warning_and_no_rerank_wording(client: Client, two_reports):
    client.force_login(create_test_user_with_active_group())
    documents = [_document(r, score=0.5) for r in two_reports]
    result = SearchResult(total_count=2, total_relation="exact", documents=documents, degraded=True)
    with patch("radis.search.views.search_provider", _provider(result)):
        response = client.get("/search/", {"query": "chest", "description": "free air"})
    content = _text(response)
    assert "Your description could not be applied (embedding service unavailable)" in content
    assert "re-ranked by your description" not in content
    assert "Re-rank score:" not in content
    assert "2 reports found" in content


@pytest.mark.django_db
def test_degraded_semantic_shows_only_the_error(client: Client):
    client.force_login(create_test_user_with_active_group())
    with patch("radis.search.views.search_provider", _provider(_empty(degraded=True))):
        response = client.get("/search/", {"mode": "semantic", "description": "free air"})
    content = _text(response)
    assert "Semantic search is unavailable right now." in content
    assert "most similar to your description" not in content
    assert "No results found" not in content


@pytest.mark.django_db
def test_a_window_hit_without_a_vector_says_so(client: Client, two_reports):
    client.force_login(create_test_user_with_active_group())
    documents = [_document(two_reports[0], similarity=0.9, score=0.8), _document(two_reports[1], score=0.4)]
    result = SearchResult(total_count=2, total_relation="exact", documents=documents)
    with patch("radis.search.views.search_provider", _provider(result)):
        response = client.get("/search/", {"query": "chest", "description": "free air"})
    assert "Similarity: not available (report not embedded yet)" in _text(response)


@pytest.mark.django_db
def test_info_card_explains_the_modes_only_with_the_capability(client: Client):
    client.force_login(create_test_user_with_active_group())
    with patch("radis.search.views.search_provider", _provider(_empty(), semantic=True)):
        with_modes = _text(client.get("/search/"))
    with patch("radis.search.views.search_provider", _provider(_empty(), semantic=False)):
        without = _text(client.get("/search/"))
    assert "Write from keywords" in with_modes and "Minimum similarity" in with_modes
    assert "Write from keywords" not in without
    assert "A minus sign does not exclude" in with_modes and "A minus sign does not exclude" in without


@pytest.mark.django_db
def test_page_sizes_are_offered(client: Client):
    client.force_login(create_test_user_with_active_group())
    with patch("radis.search.views.search_provider", _provider(_empty())):
        response = client.get("/search/", {"query": "chest"})
    assert response.context["page_sizes"] == [10, 25, 50]
```

Extend `test_search_view_returns_200_when_embedding_model_unset` with a final assertion:

```python
    assert "mode" not in response.context["form"].fields
```

and change its docstring to "SearchView renders the plain full-text page when EMBEDDINGS_MODEL is unset."

- [ ] **Step 2: Run them to verify they fail**

Run: `DJANGO_SETTINGS_MODULE=radis.settings.test uv run pytest -p no:cacheprovider radis/search/tests/test_views.py -q`
Expected: the new tests fail (`mode` never in the form, `mode_notice` missing, count-line text absent).

- [ ] **Step 3: Rewrite `SearchView.get`**

In `radis/search/views.py` replace the imports and the `get` method:

```python
from typing import Any

from adit_radis_shared.common.types import AuthenticatedHttpRequest
from django.conf import settings
from django.contrib.auth.mixins import LoginRequiredMixin, UserPassesTestMixin
from django.core.exceptions import ImproperlyConfigured
from django.core.paginator import Paginator
from django.http import Http404, HttpRequest, HttpResponseNotAllowed
from django.shortcuts import render
from django.views import View

from radis.search.forms import SearchForm
from radis.search.utils.query_parser import QueryParser

from .site import FullTextSearch, SearchRequest, SemanticSearch, SimilarityRerank, search_provider

PAGE_SIZES = [10, 25, 50]
FULL_TEXT_NEEDS_KEYWORDS = (
    "Full-text mode needs keywords. Switch to Semantic to search by description alone."
)
SEMANTIC_NEEDS_DESCRIPTION = "Semantic mode needs a description."


class SearchView(LoginRequiredMixin, UserPassesTestMixin, View):
    permission_denied_message = "You must be logged in and have an active group"
    request: AuthenticatedHttpRequest

    def test_func(self) -> bool | None:
        return self.request.user.active_group is not None

    def get(self, request: AuthenticatedHttpRequest, *args, **kwargs):
        if search_provider is None:
            raise ImproperlyConfigured("Search provider is not configured.")

        form = SearchForm(request.GET, semantic=search_provider.semantic)
        context: dict[str, Any] = {"form": form, "page_sizes": PAGE_SIZES}

        if not form.is_valid():
            return render(request, "search/search.html", context)

        page_number = self.get_page_number(request)
        page_size: int = self.get_page_size(request)

        if page_size * page_number > search_provider.max_results:
            # https://github.com/django/django/blob/6f7c0a4d66f36c59ae9eafa168b455e462d81901/django/views/generic/list.py#L76
            raise Http404(f"Invalid page {page_number}.")

        offset = (page_number - 1) * page_size
        context["offset"] = offset

        active_group = self.request.user.active_group

        # TODO: when no active group is selected show user a error
        assert active_group

        semantic_mode = form.semantic_mode()
        description = form.cleaned_data.get("description", "")
        context["mode"] = "semantic" if semantic_mode else "fulltext"
        context["description"] = description

        search: SearchRequest
        if semantic_mode:
            if not description:
                context["mode_notice"] = SEMANTIC_NEEDS_DESCRIPTION
                return render(request, "search/search.html", context)
            search = SemanticSearch(
                text=description,
                min_similarity=form.cleaned_data.get("min_similarity"),
                filters=form.to_filters(active_group.pk),
                offset=offset,
                limit=page_size,
            )
            cutoff = search.min_similarity
            if cutoff is None:
                cutoff = settings.SEARCH_SEMANTIC_MIN_SIMILARITY
            context["cutoff"] = cutoff
        else:
            # In semantic mode the keyword field is only read-only on the page, so the
            # URL may still carry it; it is only parsed here, in full-text mode.
            query_node, fixes = QueryParser().parse(form.cleaned_data["query"])
            if query_node is None:
                if description:
                    context["mode_notice"] = FULL_TEXT_NEEDS_KEYWORDS
                return render(request, "search/search.html", context)
            if len(fixes) > 0:
                context["fixed_query"] = QueryParser.unparse(query_node)
            search = FullTextSearch(
                query=query_node,
                rerank=SimilarityRerank(description) if description else None,
                filters=form.to_filters(active_group.pk),
                offset=offset,
                limit=page_size,
            )

        result = search_provider.search(search)
        context["total_count"] = result.total_count
        context["total_relation"] = result.total_relation
        context["degraded"] = result.degraded
        context["reranked"] = bool(description) and not semantic_mode and not result.degraded
        context["window"] = settings.SEARCH_RERANK_WINDOW
        context["weight"] = settings.SEARCH_RERANK_SEMANTIC_WEIGHT
        context["weight_complement"] = round(1.0 - settings.SEARCH_RERANK_SEMANTIC_WEIGHT, 2)

        # We don't allow to paginate through all results, but the provider tells
        # us how many results it can return
        max_size = min(result.total_count, search_provider.max_results)
        paginator = Paginator(range(max_size), page_size)
        context["paginator"] = paginator
        context["page_obj"] = paginator.get_page(page_number)
        context["documents"] = result.documents

        return render(request, "search/search.html", context)
```

Keep `get_page_number`, `get_page_size` and the temporary `description_view` from Task 7 as they are.

- [ ] **Step 4: Rewrite the four templates**

`radis/search/templates/search/search.html`, the results column (lines 19-25):

```html
            <div class="col">
                {% if form.non_field_errors %}
                    <div class="mt-3">{% include "bootstrap5/errors.html" %}</div>
                {% elif documents != None %}
                    {% include "search/_search_results.html" %}
                {% else %}
                    {% if mode_notice %}
                        <div class="alert alert-info mt-3" role="alert">{{ mode_notice }}</div>
                    {% endif %}
                    {% include "search/_search_info.html" %}
                {% endif %}
            </div>
```

`radis/search/templates/search/_search_results.html`:

```html
<div class="d-flex flex-column">
    {% if degraded and mode == "semantic" %}
        <div class="alert alert-danger" role="alert">Semantic search is unavailable right now.</div>
    {% else %}
        <div class="mb-3">
            <small class="ms-2">
                {% if mode == "semantic" %}
                    {% if total_count %}
                        The {{ total_count }} report{{ total_count|pluralize }} most similar to your description
                    {% else %}
                        No reports similar to your description
                    {% endif %}
                    {% if cutoff %}with similarity at least {{ cutoff|floatformat:2 }}{% endif %}
                {% else %}
                    {% if total_relation == "at_least" %}More than{% endif %}
                    {{ total_count }} report{{ total_count|pluralize }} found{% if reranked %}{% if total_count > window %}, the top {{ window }} re-ranked by your description, the rest in keyword order{% else %}, re-ranked by your description{% endif %}{% endif %}
                {% endif %}
            </small>
        </div>
        {% if degraded %}
            <div class="alert alert-warning" role="alert">
                Your description could not be applied (embedding service unavailable); results are in keyword order. Search again to retry.
            </div>
        {% endif %}
        {% if fixed_query %}
            <div class="alert alert-warning" role="alert">
                Fixed invalid query: <span class="font-monospace">{{ fixed_query }}</span>
            </div>
        {% endif %}
        {% for document in documents %}
            {% include "search/_result_document.html" %}
        {% empty %}
            {% if mode != "semantic" %}
                <div class="alert alert-light" role="alert">No results found</div>
            {% endif %}
        {% endfor %}
        <c-pagination :page-obj="page_obj" :page-sizes="page_sizes" />
    {% endif %}
</div>
```

`radis/search/templates/search/_result_header.html`, the right column:

```html
    <div>
        <div class="d-flex flex-column text-end">
            <small>Result: #{{ counter|add:offset }}</small>
            {% if document.relevance != None %}
                <small title="PostgreSQL full-text ts_rank of the keyword query">
                    Full-text rank: {{ document.relevance|floatformat:3 }}
                </small>
            {% endif %}
            {% if document.similarity != None %}
                <small title="Cosine similarity between your description and the report (1 = identical)">
                    Similarity: {{ document.similarity|floatformat:3 }}
                </small>
                {% if reranked %}
                    <small title="{{ weight }} × similarity + {{ weight_complement }} × keyword rank, each scaled 0–1 within the top {{ window }}">
                        Re-rank score: {{ document.score|floatformat:3 }}
                    </small>
                {% endif %}
            {% elif reranked and counter|add:offset <= window %}
                <small title="Newly imported reports are embedded in the background; until then they rank by keywords only">
                    Similarity: not available (report not embedded yet)
                </small>
            {% endif %}
        </div>
    </div>
```

(The unscored window hits follow the scored block, so a position within the window with no similarity is exactly a report without a vector.)

`radis/search/templates/search/_search_info.html`:

```html
<div class="card mt-3">
    <div class="card-body">
        <h5 class="card-title">Search Info</h5>
        {% if form.fields.mode %}
            <p>
                Choose a mode in the search bar. <strong>Full-text</strong> finds exactly the reports
                that contain your keywords, with the matched words highlighted. The description box
                below the keywords is optional: when you describe the reports you are looking for, the
                top hits are re-ordered by how closely they match that description, and the rest keep
                their keyword order. "Write from keywords" asks the LLM to write such a description from
                your keywords; you can edit it before searching.
            </p>
            <p>
                <strong>Semantic</strong> locks the keyword box and searches by the description alone:
                the result is the list of reports whose meaning is closest to it, in similarity order,
                without highlighting. Operators, quotes and NOT have no special meaning in a description.
                The optional "Minimum similarity" filter cuts the list where the similarity drops below
                the value you enter.
            </p>
            <p>
                Newly imported reports are embedded in the background; until then they rank by keywords
                only and do not appear in semantic mode.
            </p>
        {% else %}
            <p>
                Provide your search query in the search box above to search for reports. You can use
                additional filters to narrow down the search results (on the right side).
            </p>
        {% endif %}
        <p>The following rules apply to keywords:</p>
        <ul>
            <li>Search terms are case-insensitive.</li>
            <li>All terms in a query must be matched by a document (implicit AND query).</li>
            <li>term1 OR term2 (capital OR) means that either term1 or term2 must match.</li>
            <li>A phrase surrounded by quotes will look for an exact match, e.g. "lung disease".</li>
            <li>NOT in front of a term means that the term or phrase must not be present. A minus sign does not exclude.</li>
        </ul>
    </div>
</div>
```

- [ ] **Step 5: Carry the mode in the page's Alpine component**

In `radis/search/static/search/search.js` replace the start of `SearchForm`:

```js
function SearchForm($el) {
  return {
    // "" is full-text. The mode select fills it on init when the page has one
    // (x-model.fill); a page without an embedding model has no select and stays "".
    mode: "",
    handleSubmit() {
```

(The rest of `handleSubmit`, `resetFilters`, `QueryInput` and `RangeSlider` are unchanged.)

- [ ] **Step 6: Run the search app tests and the lint**

Run: `DJANGO_SETTINGS_MODULE=radis.settings.test uv run pytest -p no:cacheprovider radis/search -q`
Expected: all pass, including `test_views_e2e.py` (full-text default, no mode parameter).

Run: `uv run cli lint`
Expected: clean (djlint on the four templates; keep lines under 120 characters by breaking the count line's `{% if %}` chain across lines if djlint asks, as long as no whitespace lands inside the rendered phrases the tests assert).

- [ ] **Step 7: Commit**

```bash
git add radis/search
git commit -m "Let the search page choose between full-text and semantic mode" -m "The view builds a FullTextSearch, with a re-rank when a description is given, or a SemanticSearch with the per-search cutoff; the count line, the notices, the result header and the info card are worded per mode and per degradation; the page keeps the mode in its Alpine state and locks the keyword box in semantic mode." -m "Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>" -m "Claude-Session: https://claude.ai/code/session_01XTntToXdCSFQYyhezJdYvc"
```

---

### Task 9: The description writer

Spec §6.5 (writer paragraph). An async function that asks the LLM for a report-style passage about the positive keywords, cached per terms, model and prompt, with no retries and a gate budget equal to its timeout.

**Files:**
- Create: `radis/search/utils/description.py`
- Create: `radis/search/tests/conftest.py`
- Test: `radis/search/tests/test_description.py`

**Interfaces:**
- Consumes: `AsyncChatClient(feature, *, timeout)` with `chat(messages, max_completion_tokens, max_wait, transient_retry_attempts)` and `close()` (`radis/core/utils/llm_client.py`); the `SEARCH_DESCRIPTION_*` settings (Task 2).
- Produces: `radis.search.utils.description.write_description(terms: str) -> str | None` (async), `LLM_FEATURE = "query_generation"`.

- [ ] **Step 1: Add the cache-clearing fixture for the search tests**

Create `radis/search/tests/conftest.py`:

```python
import pytest
from django.core.cache import cache


@pytest.fixture(autouse=True)
def _clear_cache():
    """The description writer and the ranking cache use the process-local test cache,
    which outlives individual tests; a cached answer from one test must not serve the
    next."""
    cache.clear()
    yield
    cache.clear()
```

- [ ] **Step 2: Write the failing writer tests**

Create `radis/search/tests/test_description.py`:

```python
import logging
from unittest.mock import AsyncMock, patch

import pytest

from radis.chats.utils.testing_helpers import make_connection_error
from radis.core.utils.llm_client import LLMResponseError
from radis.core.utils.model_spec import ModelSpec
from radis.core.utils.rate_limit import RateLimited
from radis.search.utils import description as writer

ANSWER = '"Kleiner apikaler Pneumothorax rechts. Kein Pleuraerguss."'
CLEANED = "Kleiner apikaler Pneumothorax rechts. Kein Pleuraerguss."


@pytest.fixture
def llm():
    with patch("radis.search.utils.description.AsyncChatClient") as client_cls:
        client = AsyncMock()
        client.chat.return_value = ANSWER
        client_cls.return_value = client
        yield client_cls, client


@pytest.mark.asyncio
async def test_returns_the_cleaned_passage_for_the_collapsed_terms(llm):
    result = await writer.write_description(" pneumothorax \n rechts ")

    assert result == CLEANED
    _, client = llm
    messages = client.chat.call_args.args[0]
    assert messages[0]["role"] == "system"
    assert "radiology reports" in messages[0]["content"]
    assert messages[1] == {"role": "user", "content": "pneumothorax rechts"}


@pytest.mark.asyncio
async def test_uses_the_query_generation_feature_with_its_own_budget(llm, settings):
    settings.SEARCH_DESCRIPTION_TIMEOUT = 7
    settings.SEARCH_DESCRIPTION_MAX_TOKENS = 50

    await writer.write_description("pneumothorax")

    client_cls, client = llm
    client_cls.assert_called_once_with("query_generation", timeout=7)
    assert client.chat.call_args.kwargs == {
        "max_completion_tokens": 50,
        "max_wait": 7.0,
        "transient_retry_attempts": 0,
    }
    client.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_answers_are_cached(llm):
    assert await writer.write_description("pneumothorax") == CLEANED
    assert await writer.write_description("pneumothorax") == CLEANED

    _, client = llm
    assert client.chat.call_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure",
    [make_connection_error(), RateLimited(), LLMResponseError("empty")],
    ids=["connection", "rate-limited", "no-content"],
)
async def test_failures_degrade_to_none_and_are_not_cached(llm, failure, caplog):
    _, client = llm
    client.chat.side_effect = [failure, ANSWER]

    with caplog.at_level(logging.WARNING, logger="radis.search.utils.description"):
        first = await writer.write_description("pneumothorax")
    second = await writer.write_description("pneumothorax")

    assert first is None
    assert second == CLEANED
    assert client.chat.call_count == 2
    assert "description writer failed" in caplog.text
    assert client.close.await_count == 2


@pytest.mark.asyncio
async def test_blank_terms_never_call_the_llm(llm):
    assert await writer.write_description("   ") is None
    _, client = llm
    client.chat.assert_not_called()


@pytest.mark.asyncio
async def test_a_blank_answer_counts_as_failure(llm):
    _, client = llm
    client.chat.side_effect = ['""', ANSWER]

    assert await writer.write_description("pneumothorax") is None
    assert await writer.write_description("pneumothorax") == CLEANED


@pytest.mark.asyncio
async def test_the_cache_key_covers_the_prompt_and_the_model(llm, settings):
    _, client = llm
    await writer.write_description("pneumothorax")
    settings.SEARCH_DESCRIPTION_SYSTEM_PROMPT = "Another instruction."
    await writer.write_description("pneumothorax")
    settings.LLM_MODELS = {"query_generation": ModelSpec("other-model", {})}
    await writer.write_description("pneumothorax")

    assert client.chat.call_count == 3
```

- [ ] **Step 3: Run them to verify they fail**

Run: `DJANGO_SETTINGS_MODULE=radis.settings.test uv run pytest -p no:cacheprovider radis/search/tests/test_description.py -v`
Expected: `ModuleNotFoundError: No module named 'radis.search.utils.description'`.

- [ ] **Step 4: Write the module**

Create `radis/search/utils/description.py`:

```python
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

    client = AsyncChatClient(LLM_FEATURE, timeout=settings.SEARCH_DESCRIPTION_TIMEOUT)
    try:
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
        await client.close()

    description = " ".join(answer.split()).strip().strip('"')
    if not description:
        return None
    await cache.aset(key, description, timeout=settings.SEARCH_DESCRIPTION_CACHE_TIMEOUT_SECONDS)
    return description
```

- [ ] **Step 5: Run the tests and the lint**

Run: `DJANGO_SETTINGS_MODULE=radis.settings.test uv run pytest -p no:cacheprovider radis/search/tests/test_description.py -v`
Expected: all pass. Run `uv run cli lint`; expected clean.

- [ ] **Step 6: Commit**

```bash
git add radis/search/utils/description.py radis/search/tests/conftest.py radis/search/tests/test_description.py
git commit -m "Add the LLM description writer for the search page" -m "Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>" -m "Claude-Session: https://claude.ai/code/session_01XTntToXdCSFQYyhezJdYvc"
```

---

### Task 10: The description endpoint

Spec §6.5 (endpoint paragraphs). An async HTMX view behind "Write from keywords" that re-renders the description row with the LLM's answer or a notice, touching no ORM in its body.

**Files:**
- Modify: `radis/search/views.py` (replace the Task 7 placeholder `description_view`)
- Modify: `radis/search/urls.py`
- Test: `radis/search/tests/test_description_view.py` (new)

**Interfaces:**
- Consumes: `write_description` (Task 9), `DescriptionRequestForm`, `DESCRIPTION_MAX_LENGTH` (Task 7), `QueryParser.unparse_positive_terms` (Task 5), the partial `description_input.html` (Task 7).
- Produces: `POST /search/description/` named `search_description`; responses render `search/form_elements/description_input.html` with `value` and `notice`; constants `ENTER_KEYWORDS`, `WRITER_FAILED` in `radis/search/views.py`.

- [ ] **Step 1: Write the failing endpoint tests**

Create `radis/search/tests/test_description_view.py`:

```python
from unittest.mock import AsyncMock, patch

import pytest
from adit_radis_shared.accounts.factories import GroupFactory, UserFactory
from django.test import Client
from django.urls import reverse

HX_HEADERS = {"HX-Request": "true"}
URL_NAME = "search_description"


@pytest.fixture(autouse=True)
def _disable_debug_toolbar(settings):
    """example.env forces the toolbar on; it fails to reverse its URLs under test."""
    settings.DEBUG_TOOLBAR_CONFIG = {"SHOW_TOOLBAR_CALLBACK": lambda request: False}


@pytest.fixture
def writer():
    with patch("radis.search.views.write_description", new=AsyncMock(return_value="Pneumothorax rechts.")) as mock:
        yield mock


def _login(client: Client, *, with_group: bool = True):
    user = UserFactory.create(is_active=True)
    if with_group:
        group = GroupFactory.create()
        user.groups.add(group)
        user.active_group = group
        user.save()
    client.force_login(user)
    return user


def _post(client: Client, query: str, description: str = "", htmx: bool = True):
    return client.post(
        reverse(URL_NAME),
        {"query": query, "description": description},
        headers=HX_HEADERS if htmx else {},
    )


@pytest.mark.django_db
def test_anonymous_is_redirected_to_login(client: Client, writer):
    response = _post(client, "pneumothorax")
    assert response.status_code == 302
    assert "/accounts/login/" in response["Location"]
    writer.assert_not_called()


@pytest.mark.django_db
def test_get_is_not_allowed(client: Client, writer):
    _login(client)
    assert client.get(reverse(URL_NAME), headers=HX_HEADERS).status_code == 405


@pytest.mark.django_db
def test_non_htmx_post_is_rejected(client: Client, writer):
    _login(client)
    assert _post(client, "pneumothorax", htmx=False).status_code == 400
    writer.assert_not_called()


@pytest.mark.django_db
def test_a_user_without_an_active_group_gets_403(client: Client, writer):
    _login(client, with_group=False)
    assert _post(client, "pneumothorax").status_code == 403
    writer.assert_not_called()


@pytest.mark.django_db
def test_fills_the_textarea_with_the_positive_terms_description(client: Client, writer):
    _login(client)
    response = _post(client, "pneumothorax NOT rechts", description="old draft")

    assert response.status_code == 200
    content = response.content.decode()
    assert 'id="id_description"' in content
    assert ">Pneumothorax rechts.</textarea>" in content
    assert 'id="description_notice"' in content and 'hx-swap-oob="true"' in content
    assert "could not write" not in content
    writer.assert_awaited_once_with("pneumothorax")


@pytest.mark.django_db
def test_failure_keeps_the_submitted_text_and_shows_the_notice(client: Client, writer):
    writer.return_value = None
    _login(client)
    response = _post(client, "pneumothorax", description="my draft")

    content = response.content.decode()
    assert ">my draft</textarea>" in content
    assert "The LLM could not write a description right now." in content


@pytest.mark.django_db
@pytest.mark.parametrize("query", ["", "NOT pneumothorax", '"" AND ()'])
def test_unparseable_keywords_get_the_enter_keywords_notice(client: Client, writer, query):
    _login(client)
    response = _post(client, query, description="kept")

    content = response.content.decode()
    assert "Enter search keywords first; the description is written from the positive terms." in content
    assert ">kept</textarea>" in content
    writer.assert_not_called()


@pytest.mark.django_db
def test_an_over_long_query_gets_the_failure_notice_without_an_llm_call(client: Client, writer):
    _login(client)
    response = _post(client, "a" * 201)

    assert "The LLM could not write a description right now." in response.content.decode()
    writer.assert_not_called()


@pytest.mark.django_db
def test_the_answer_is_truncated_to_the_field_limit(client: Client, writer):
    writer.return_value = "x" * 1500
    _login(client)
    response = _post(client, "pneumothorax")

    assert (">" + "x" * 1000 + "</textarea>") in response.content.decode()


@pytest.mark.django_db
def test_the_view_touches_no_sync_database_api(client: Client, writer, monkeypatch):
    """The acceptance conftests set DJANGO_ALLOW_ASYNC_UNSAFE for the whole process,
    which would mask a sync ORM call in the async body; without it such a call raises
    SynchronousOnlyOperation under the test client exactly as it would under Daphne."""
    monkeypatch.delenv("DJANGO_ALLOW_ASYNC_UNSAFE", raising=False)
    _login(client)
    assert _post(client, "pneumothorax").status_code == 200
```

- [ ] **Step 2: Run them to verify they fail**

Run: `DJANGO_SETTINGS_MODULE=radis.settings.test uv run pytest -p no:cacheprovider radis/search/tests/test_description_view.py -v`
Expected: the placeholder view answers 405 to every POST, so the content assertions fail (and `write_description` is not a name in `radis.search.views` yet, so the fixture's patch fails first).

- [ ] **Step 3: Write the view and route**

In `radis/search/views.py` replace the placeholder with:

```python
from asgiref.sync import sync_to_async
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied, SuspiciousOperation
from django.http import HttpResponse
from django.views.decorators.http import require_POST

from radis.search.forms import DESCRIPTION_MAX_LENGTH, DescriptionRequestForm, SearchForm
from radis.search.utils.description import write_description

ENTER_KEYWORDS = "Enter search keywords first; the description is written from the positive terms."
WRITER_FAILED = "The LLM could not write a description right now."


async def _description_response(request: HttpRequest, value: str, notice: str) -> HttpResponse:
    # Context processors may touch the sync ORM or session APIs, so the render runs
    # off the event loop.
    return await sync_to_async(render)(
        request,
        "search/form_elements/description_input.html",
        {"value": value, "notice": notice},
    )


@require_POST
@login_required
async def description_view(request: AuthenticatedHttpRequest) -> HttpResponse:
    """HTMX endpoint behind "Write from keywords": re-renders the description row
    with an LLM-written passage about the positive keywords, or with a notice.

    Async so the LLM call does not occupy a worker thread. The body touches only
    ``request.auser()`` and an ORM-free form; a sync database call here would raise
    ``SynchronousOnlyOperation`` under ASGI.
    """
    if not request.htmx:
        raise SuspiciousOperation

    user = await request.auser()
    if user.active_group_id is None:  # type: ignore[attr-defined]
        raise PermissionDenied

    form = DescriptionRequestForm(request.POST)
    submitted = request.POST.get("description", "")[:DESCRIPTION_MAX_LENGTH]
    if not form.is_valid():
        return await _description_response(request, submitted, WRITER_FAILED)
    submitted = form.cleaned_data["description"]

    node, _fixes = QueryParser().parse(form.cleaned_data["query"])
    terms = QueryParser.unparse_positive_terms(node) if node is not None else ""
    if not terms.strip():
        return await _description_response(request, submitted, ENTER_KEYWORDS)

    description = await write_description(terms)
    if description is None:
        return await _description_response(request, submitted, WRITER_FAILED)
    return await _description_response(request, description[:DESCRIPTION_MAX_LENGTH], "")
```

Drop the `HttpResponseNotAllowed` import from Task 7. If pyright accepts `user.active_group_id` without the ignore (the shared user model declares the field), remove the ignore comment, because an unnecessary one fails the lint.

In `radis/search/urls.py`:

```python
from django.urls import path
from rest_framework.urlpatterns import format_suffix_patterns

from .views import SearchView, description_view

urlpatterns = [
    path("", SearchView.as_view(), name="search"),
    path("description/", description_view, name="search_description"),
]

urlpatterns = format_suffix_patterns(urlpatterns)
```

- [ ] **Step 4: Run the search tests and the lint**

Run: `DJANGO_SETTINGS_MODULE=radis.settings.test uv run pytest -p no:cacheprovider radis/search -q`
Expected: all pass. If `test_the_view_touches_no_sync_database_api` raises `SynchronousOnlyOperation`, the body touched the ORM: look for a `request.user` access or a form with database-backed fields.

Run: `uv run cli lint`
Expected: clean.

- [ ] **Step 5: Commit**

```bash
git add radis/search
git commit -m "Add the description endpoint behind the write-from-keywords button" -m "An async HTMX view binds the keyword query to an ORM-free form, feeds the positive terms to the writer, and swaps the description row back with the answer or a notice; anonymous, non-HTMX and group-less requests are refused before any LLM call." -m "Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>" -m "Claude-Session: https://claude.ai/code/session_01XTntToXdCSFQYyhezJdYvc"
```

---

### Task 11: Documentation and wording sweep

Spec §8. Every document and template that describes fusion, RRF, a silent FTS-only fallback or a page without a mode select is brought in line. No behaviour changes; the lint (djlint for templates) is the only automated check, plus a grep sweep at the end.

**Files:**
- Modify: `AGENTS.md` (symlinked as `CLAUDE.md` and `GEMINI.md`; edit the one file), `README.md`, `docs/index.md`, `docs/user-docs/user-guide.md`, `docs/user-docs/admin-guide.md`, `docs/dev-docs/architecture.md`, `docs/dev-docs/contributing.md`, `radis/core/templates/core/home.html`, `TODO.md`, `docs/superpowers/specs/hybrid-search.md`, `docs/superpowers/specs/2026-08-29-fts-query-shape-performance-design.md`, `radis/core/tests/test_embeddings_settings.py:45`

- [ ] **Step 1: `AGENTS.md`**

Make these replacements (line numbers from `main`):
- Line 7, "It features hybrid search (full-text + semantic), LLM integration" → "It features full-text search with an optional similarity re-rank, a separate semantic search mode, LLM integration".
- Line 24: `uv run cli lint                      # Run linting (ruff + pyright + djlint)`.
- Line 45: `- **Search**: PostgreSQL full-text search (tsvector, ts_rank); pg_vector for the semantic mode and the similarity re-rank of full-text hits`.
- Line 55: `- **radis.search/**: Full-text and semantic search interface. Contains \`SearchView\`, \`SearchForm\`, the request types \`FullTextSearch\` and \`SemanticSearch\`, \`QueryParser\` for complex query syntax, the provider registry, and the description endpoint behind the search page's "Write from keywords" button.`
- Line 56: `- **radis.pgsearch/**: PostgreSQL search implementation. One \`search(request)\` entry point serves full-text searches (exactly the keyword hits, optionally re-ranked within a window by a description) and semantic searches (HNSW nearest neighbours), with a two-layer ranking cache.`
- Lines 80-81, replace both bullets with:

  ```
  - **Two modes, nothing fused**: Full-text (default) returns exactly the keyword hits ranked by `ts_rank`; with a description the top `SEARCH_RERANK_WINDOW` hits are re-ordered by `w · norm(similarity) + (1 − w) · norm(ts_rank)` (`w` = `SEARCH_RERANK_SEMANTIC_WEIGHT`) and the rest follow in keyword order. Semantic returns the `SEARCH_HNSW_EF_SEARCH` reports nearest to the description under the same filters, in similarity order, cut at `SEARCH_SEMANTIC_MIN_SIMILARITY` when set
  - **Degradation**: an embedding failure flags the result degraded instead of changing modes; the page says so
  ```
- Line 106, append to the per-feature override bullet: ` (\`LLM_QUERY_GENERATION_MODEL\` also serves the search page's description writer)`.
- Line 111: `Embeddings and search tuning (\`radis.pgsearch\`):`; line 114: `  models. Empty means full-text search only — no embedding jobs, no calls to the service, and no mode select, description or cutoff on the search page`; lines 121-122: `- \`EMBEDDINGS_QUERY_INSTRUCTION\`: Instruction prefix prepended to the description text that is embedded.`
- After line 130 add:

  ```
  - `EMBEDDINGS_QUERY_REQUEST_TIMEOUT_SECONDS`: Request timeout of a query embedding on the search
    path (default `10`s); report embedding keeps `EMBEDDINGS_REQUEST_TIMEOUT_SECONDS`
  - `SEARCH_HNSW_EF_SEARCH`: HNSW beam width and the number of reports a semantic search lists
    (default `500`, 1..1000, carried as a libpq connection option)
  - `SEARCH_RANKING_CACHE_TIMEOUT_SECONDS`: How long a ranking stays cached (default `300`s,
    `0` disables); new reports join a cached query's results when it expires
  - `SEARCH_RERANK_WINDOW`, `SEARCH_RERANK_SEMANTIC_WEIGHT`: Full-text hits re-ranked by a
    description (default `1000`) and the weight of similarity in the score (default `0.7`)
  - `SEARCH_SEMANTIC_ITERATIVE_SCAN`: Resume the index scan under selective filters
    (`hnsw.iterative_scan = strict_order`, default `true`)
  - `SEARCH_SEMANTIC_MIN_SIMILARITY`: Similarity cutoff for semantic mode, 0..1, unset by
    default; users can override it per search
  - `SEARCH_DESCRIPTION_TIMEOUT`, `SEARCH_DESCRIPTION_MAX_TOKENS`,
    `SEARCH_DESCRIPTION_CACHE_TIMEOUT_SECONDS`, `SEARCH_DESCRIPTION_SYSTEM_PROMPT`: The
    "Write from keywords" writer (defaults `10`s, `200`, `3600`s, built-in prompt)
  ```
- Line 178: `- **Linting**: Ruff with E, F, I, DJ, UP rules`.
- Lines 232-243 ("### Search via API" and its code block): delete; `radis/reports/api` has no search endpoint.
- Line 257: `chat, query generation and the search page's description writer run in \`web\``.
- Lines 267-274, retitle `### Semantic Mode Missing or Re-rank Not Applied` and replace the first and the fourth bullet:

  ```
  - Confirm `EMBEDDINGS_MODEL` is set — empty is the documented way to run full-text only, and the
    search page then has no mode select, description or cutoff
  ...
  - A search whose embedding fails is flagged degraded: full-text mode shows a warning and keeps
    keyword order, semantic mode shows an error and no list; the `web` log names the cause. A
    query embedding waits at most `EMBEDDINGS_QUERY_REQUEST_TIMEOUT_SECONDS` plus the gate budget
  ```

- [ ] **Step 2: `README.md`, `docs/index.md`, `home.html`, `TODO.md`**

- `README.md` line 38: `- **Advanced Text Search**: Full-text search with boolean operators and metadata filters, plus a semantic mode that finds reports by a description and an optional similarity re-rank of full-text hits when an embedding model is configured.`
- `README.md` line 60, second sentence: `By default, RADIS uses PostgreSQL's built-in full-text search and, when an embedding model is configured, a separate semantic search mode through the pg_vector extension.`
- `docs/index.md` line 51: `2. **Advanced Search**: Full-text search with filters, or a semantic search by description when an embedding model is configured`
- `radis/core/templates/core/home.html` lines 24-27: `... collect radiology reports. For searching relevant reports it offers full-text search and, when configured, semantic search by description.`; line 31: `<li>Full-text search with boolean operators, and semantic search by description</li>`.
- `TODO.md`: delete the five lines of the "Look into distanceThreshold" entry (51-55).

- [ ] **Step 3: `docs/user-docs/user-guide.md`**

- Line 11: `3. **RADIS processes** your query with full-text search by default, or, when your administrator has configured it, with a semantic search that finds the reports whose meaning is closest to your description`
- Lines 31-34 (the four steps) become:

  ```
  1. Navigate to the "Search" section
  2. Choose the mode in the search bar: Full-text (the default) or Semantic (offered when your administrator has configured an embedding model)
  3. Enter your keywords (full-text) or describe the reports you are looking for (semantic). In full-text mode the description is optional and re-orders the top hits; "Write from keywords" lets the LLM write it for you, and you can edit it before searching
  4. Apply filters as needed on the right-hand side
  5. Review the search results
  ```
- Line 47, last sentence: `In full-text mode a query is required (filters alone do not search) and may be at most 200 characters long; a description may be at most 1,000 characters.`
- Lines 51-53, the "Semantic Search" section:

  ```
  #### Semantic Search

  When your administrator has configured an embedding model, the search bar offers two modes. **Full-text** finds exactly the reports that contain your keywords and highlights them. If you also describe the reports you are looking for in the box below the keywords, the top 1,000 hits are re-ordered by how closely each one matches your description, and the rest keep their keyword order; the count line says so. **Semantic** locks the keyword box and searches by the description alone: the result is the list of reports whose meaning is closest to it, in similarity order, without highlighting. Operators, quotes and NOT have no special meaning in a description.

  "Write from keywords" asks the LLM to write a short report-style description from your keywords; it ignores negated terms, and you can edit the text before searching. Reports imported recently may not be embedded yet: they rank by keywords only, are listed after the re-ordered hits with the note "not embedded yet", and do not appear in semantic mode. If the embedding service is unavailable, a notice says so: full-text results stay in keyword order, and semantic mode shows no list.

  The description travels in the page address like the keywords and appears in your browser history and in server logs, so do not type patient identifiers into it.
  ```
- Filters list, after the Age Range bullet: `- **Minimum similarity** (semantic mode only): cut the list where the similarity to your description drops below this value (0 to 1). Leave it empty for your administrator's default, or enter 0 to switch the cutoff off for one search`
- Line 69: `Each result shows the patient age and sex, modalities, study description and the scores that determined its rank: the full-text rank of the keywords, the similarity to your description when one was given, and the re-rank score inside the re-ordered block; in semantic mode only the similarity. Full-text results show a highlighted excerpt, semantic results the opening of the report. Click "Show full report" to read the entire text. Results are paginated; the page size can be changed below the list. Each result panel has buttons to open the report details, add the report to a collection, add a note, start a chat about the report, and open the study in your PACS viewer.`

- [ ] **Step 4: `docs/user-docs/admin-guide.md`**

- Line 32: `the \`EMBEDDINGS_*\` and \`SEARCH_*\` groups (see [Embeddings and search](#embeddings-and-search))`; line 75: the same anchor.
- Line 68: `Pay particular attention to the \`LLM_*\`, \`EMBEDDINGS_*\`, \`SEARCH_*\`, \`LABELING_*\`, and \`ANALYSIS_*\` groups`.
- Line 82: `All AI features (chats, extractions, subscriptions, labeling, query generation, the search page's description writer) send their requests ...`
- Line 91, append: ` \`LLM_QUERY_GENERATION_MODEL\` also serves the search page's "Write from keywords" button`.
- Lines 103-105:

  ```
  ### Embeddings and search

  By default RADIS runs full-text search only. Setting `EMBEDDINGS_MODEL` adds a Semantic mode to the search page (the reports nearest to a description, in similarity order) and lets a description re-order the top full-text hits; full-text stays the default and nothing is fused:
  ```
- Line 110: `- \`EMBEDDINGS_QUERY_INSTRUCTION\`: Model-specific instruction prefix for the description text that is embedded (some models, e.g. Qwen3-Embedding, want one)`
- Line 113, append: ` Until then they never appear in semantic mode and, in a re-ordered full-text search, are listed after the re-ordered hits with the note "not embedded yet".`
- Line 117: `When the embedding service is unreachable or rate limiting, the search is flagged degraded: full-text mode shows a warning and keeps keyword order, semantic mode shows an error instead of a list, and a warning is logged in the \`web\` service. A query embedding waits at most \`EMBEDDINGS_QUERY_REQUEST_TIMEOUT_SECONDS\` (10 s by default) plus the rate-limit gate's query budget.`
- After line 119 add:

  ```
  ### Search tuning

  All optional, with defaults that fit a corpus of a few million reports:

  - `SEARCH_HNSW_EF_SEARCH`: HNSW beam width and the number of reports a semantic search lists (default `500`, 1..1000)
  - `SEARCH_SEMANTIC_ITERATIVE_SCAN`: Resume the index scan under selective filters so a small group or a rare modality still fills the list (default `true`, about 2.4× the cost of one pass on unfiltered searches)
  - `SEARCH_SEMANTIC_MIN_SIMILARITY`: Similarity cutoff (0..1) for semantic mode; unset by default because similarities are comparable only within one model and corpus. Try values with the page's "Minimum similarity" field before setting a default
  - `SEARCH_RERANK_WINDOW`, `SEARCH_RERANK_SEMANTIC_WEIGHT`: How many top full-text hits a description re-orders (default `1000`) and the weight of similarity against the keyword rank (default `0.7`; `1` orders the window by similarity alone)
  - `SEARCH_RANKING_CACHE_TIMEOUT_SECONDS`: How long a ranking stays cached (default `300`s, `0` disables); reports created or updated within that time join a cached query's results when it expires
  - `SEARCH_DESCRIPTION_TIMEOUT`, `SEARCH_DESCRIPTION_MAX_TOKENS`, `SEARCH_DESCRIPTION_CACHE_TIMEOUT_SECONDS`, `SEARCH_DESCRIPTION_SYSTEM_PROMPT`: The "Write from keywords" writer; a button press waits at most about twice the timeout (default `10`s)

  The production cache table holds rankings, query vectors and written descriptions; it is sized at 2,000 entries (`MAX_ENTRIES` in `radis/settings/production.py`) because Django's default of 300 would evict hot rankings at random.
  ```

- [ ] **Step 5: `docs/dev-docs/architecture.md` and `contributing.md`**

- architecture.md lines 59-61: `generating and storing report vectors for the semantic search mode and the similarity re-rank, including operator backfills ...`
- Line 75: `**Embeddings**: Semantic search and the full-text re-rank add a second external service, an OpenAI-compatible ...` (the rest of the paragraph stays).
- Lines 90-95:

  ```
  **Search Provider Interface**: `radis/search/site.py` defines the request types and the registry. `Search` (filters, offset, limit) is the base; `FullTextSearch` carries the parsed keyword query and an optional `SimilarityRerank(text)`; `SemanticSearch` carries the description text and an optional per-search minimum similarity. A provider registers one `search(request)` callable, its paging cap and whether semantic search is available; extractions register `count` and `retrieve` over `FullTextSearch`; subscriptions register `filter` over `SearchFilters`. A `SearchResult` carries the count, its relation to the true total, the documents (full-text rank, similarity, score, an HTML-safe summary) and a `degraded` flag.

  - **PgSearch Provider**: Default implementation using PostgreSQL full-text search and pg_vector
  - **Alternative Providers**: Other engines such as Vespa or ElasticSearch can be integrated through the same interface
  ```
- Line 113:

  ```
  **Two modes**: A full-text search runs the single-table candidate query (up to `SEARCH_FTS_MAX_RESULTS` keyword hits ranked by `ts_rank` under each document's own text-search configuration) and returns exactly those hits. With a description, the top `SEARCH_RERANK_WINDOW` hits are measured against its embedding in one id lookup and re-ordered by `w · norm(similarity) + (1 − w) · norm(ts_rank)`; hits beyond the window and hits without a vector follow in keyword order. A semantic search embeds the description and runs one HNSW scan under the same filters, capped at `SEARCH_HNSW_EF_SEARCH`, inside a transaction with `hnsw.iterative_scan = strict_order` when `SEARCH_SEMANTIC_ITERATIVE_SCAN` is on; the list is cut at the effective minimum similarity after retrieval. Rankings are cached in layers (`SEARCH_RANKING_CACHE_TIMEOUT_SECONDS`): the keyword layer always, the window layer and semantic lists only when the embedding succeeded. An embedding failure flags the result `degraded`; a query embedding is bounded by `EMBEDDINGS_QUERY_REQUEST_TIMEOUT_SECONDS`. Summaries are escaped in Python; the highlight markers are control characters restored to `<em>` after escaping.
  ```
- contributing.md lines 106-110: `The semantic search mode and the description re-rank need a second model, an embedding model, reached over the same kind of OpenAI-compatible endpoint. It is optional: with \`EMBEDDINGS_MODEL\` empty, search runs full-text only, the search page shows no mode select, and nothing else changes — no queued jobs, no failed calls.`
- contributing.md line 149, append after `LLM_LABELING_MODEL`: ` (\`LLM_QUERY_GENERATION_MODEL\` also drives the search page's description writer)`.

- [ ] **Step 6: History documents and the test name**

- `docs/superpowers/specs/hybrid-search.md`: insert after line 3:

  ```
  **Superseded 2026-10-22** by `docs/superpowers/specs/2026-10-22-search-modes-design.md` for §3 steps 1–5, §7.1–§7.4, §7.6–§7.8, §8.2, the query-path rows and the two non-policies of §9, the query-path test rows of §10, §11.5 and §11.7. Hybrid fusion no longer exists; the search page offers a full-text mode with an optional similarity re-rank and a separate semantic mode. The ingestion and embedding pipeline described here (§4–§6, §7.5, §8.1) remains authoritative.
  ```
- `docs/superpowers/specs/2026-08-29-fts-query-shape-performance-design.md`, after the "Consumers." paragraph (line 271): `*(2026-10-22: `_fuse_hybrid` and `_exclude_negations` were removed by the search modes design; `search()`, `count()` and `retrieve()` now route through the keyword ranking, which runs this same candidate query.)*`
- `radis/core/tests/test_embeddings_settings.py` line 45: rename the function to `test_no_model_configured_means_semantic_search_is_off`.

- [ ] **Step 7: Sweep and lint**

Run: `git grep -n -i 'hy[a-z]*brid\|rrf\|fused\|FTS-only\|full-text-only' -- radis docs AGENTS.md README.md example.env TODO.md`
Expected: hits only in `docs/superpowers/` history files, in `radis/pgsearch/migrations/0002_hybrid_search.py` (immutable), and in `radis/pgsearch/tasks.py` / `embed_pending.py`, where "FTS-only" describes a deployment without an embedding model and stays. Anything else is a leftover: fix it.

Run: `uv run cli lint` (djlint checks `home.html`); expected clean. Run the search and pgsearch suites once more; expected all pass.

- [ ] **Step 8: Commit**

```bash
git add AGENTS.md README.md docs TODO.md radis/core/templates/core/home.html radis/core/tests/test_embeddings_settings.py
git commit -m "Document the two search modes and retire the hybrid wording" -m "Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>" -m "Claude-Session: https://claude.ai/code/session_01XTntToXdCSFQYyhezJdYvc"
```

---

### Task 12: Verification, staging measurements and the PR

Spec §10, §12. The whole suite and the lint gate, the branch on the side stack with the real corpus, the measurements that fix the iterative-scan default, and the pull request. Ids and numbers only; no report text is read.

**Files:**
- Modify (only if the measurement flips the default): `radis/settings/base.py` (`SEARCH_SEMANTIC_ITERATIVE_SCAN`), `example.env`, `docs/superpowers/specs/2026-10-22-search-modes-design.md` §7 row, `AGENTS.md`, `docs/user-docs/admin-guide.md`
- Create (scratch, not committed): a measurement script under the scratchpad directory

- [ ] **Step 1: Run everything**

Run: `DJANGO_SETTINGS_MODULE=radis.settings.test uv run pytest -p no:cacheprovider radis -q -m "not acceptance"`
Expected: all pass. Then `uv run cli lint`; expected clean. Then `git status --short`; expected empty.

- [ ] **Step 2: Build and deploy the branch to the side stack**

The side stack `radis_spike` (web only, port 8503, sharing the staging database and `.env`) exists from the spike; its compose file `docker-compose.spike.yml` lives untracked in the spike worktree. Copy it into this worktree (it stays untracked), then from this worktree:

```bash
cp /projects/deploy/radis_staging/.claude/worktrees/hybrid-fusion-spikes/docker-compose.spike.yml .
docker build --target production --build-arg PROJECT_VERSION=0.0.0 -t radis_spike:latest .
docker service update --force radis_spike_web
docker service ps radis_spike_web --no-trunc | head -3
```

Before the first build check `git diff --stat 2f0df7b9 origin/main -- '*/migrations/*'` is empty (2f0df7b9 is the commit the staging stack was built from; confirm it against the deployment notes if in doubt) (this branch adds no migration, so sharing the staging database stays safe). Probe from inside the container: `docker exec $(docker ps -qf name=radis_spike_web) curl -sk https://10.20.24.202:8503/health/`; expected `200`. Then try the page at `https://10.20.24.202:8503/search/` with a staging login: both modes, the write button, the cutoff field, a degraded notice (stop the embedding endpoint briefly or point `EMBEDDINGS_BASE_URL` at a dead host in the stack's override), and pagination carrying the description.

- [ ] **Step 3: Measure semantic latency with the scan on and off**

Write the script to the scratchpad directory and run it inside the container with `docker exec $(docker ps -qf name=radis_spike_web) ./manage.py shell -c "$(cat script.py)"`. It must print only timings, counts and ids:

```python
import time
from django.contrib.auth.models import Group
from radis.search.site import SearchFilters, SemanticSearch, FullTextSearch, SimilarityRerank
from radis.pgsearch import providers
from radis.search.utils.query_parser import QueryParser
from django.core.cache import cache
from django.conf import settings

GROUP = Group.objects.order_by("pk").first().pk  # the staging group; confirm its name in the admin
TEXTS = ["Pneumothorax links apikal", "frische Wirbelkörperfraktur", "Hirnblutung", "Lebermetastasen"]

def timed(request):
    cache.clear()
    t0 = time.perf_counter(); result = providers.search(request); dt = time.perf_counter() - t0
    return round(dt * 1000), result.total_count, result.degraded

for text in TEXTS:
    for filters in (SearchFilters(group=GROUP), SearchFilters(group=GROUP, modalities=["MR"])):
        for iterative in (True, False):
            settings.SEARCH_SEMANTIC_ITERATIVE_SCAN = iterative
            ms, count, degraded = timed(SemanticSearch(text=text, filters=filters, limit=25))
            print(f"semantic text={text!r} modalities={filters.modalities} iterative={iterative}: {ms} ms, {count} results, degraded={degraded}")
settings.SEARCH_SEMANTIC_ITERATIVE_SCAN = True
node, _ = QueryParser().parse("Fraktur")
ms, count, degraded = timed(FullTextSearch(query=node, filters=SearchFilters(group=GROUP), rerank=SimilarityRerank("frische Wirbelkörperfraktur nach Sturz"), limit=25))
print(f"fulltext 'Fraktur' with window re-rank: {ms} ms, {count} hits, degraded={degraded}")
ms, count, degraded = timed(FullTextSearch(query=node, filters=SearchFilters(group=GROUP), limit=25))
print(f"fulltext 'Fraktur' plain: {ms} ms, {count} hits")
```

Also run `EXPLAIN ANALYZE` of the semantic scan once on the real corpus (take the SQL from `providers._semantic_scan_queryset(providers._build_filter_query(SearchFilters(group=GROUP)), vec).query` and run it in `./manage.py dbshell`); expected: `Index Scan using pgsearch_embedding_hnsw`.

Decision rule: keep `SEARCH_SEMANTIC_ITERATIVE_SCAN = True` if the unfiltered semantic search stays under about 300 ms and the filtered searches return markedly more results with the scan on; otherwise flip the default to `False` in `base.py`, `example.env`, the spec's §7 row, `AGENTS.md` and the admin guide, and commit that change ("Default the semantic scan to one pass after measuring on staging").

- [ ] **Step 4: Measure the similarity ranges for a first cutoff**

With the same script shape, for each of the spike's benchmark queries (Glioblastom, UIP, Mamma Ca, Pankreaskarzinom, Hepatomegalie, Hirnblutung, Tethered Cord, Osteoporose, instabile Fraktur, Darmischämie, Pneumothorax, Lungenembolie, Appendizitis, Spondylodiszitis) run a `SemanticSearch` with a one-sentence description and print the similarity at ranks 1, 10, 50, 100, 250 and 500 (`result.documents[i].similarity` while paging with `limit=500`). Record the table in the PR description. Do not set `SEARCH_SEMANTIC_MIN_SIMILARITY` yet; the numbers are the basis for the owner's choice.

- [ ] **Step 5: Open the pull request**

```bash
git push -u origin search-modes
gh pr create --title "Two search modes: full-text with a description re-rank, and semantic" --body-file /tmp/claude-1357/-projects-deploy-radis-staging/c78e4649-df03-47f4-a8b9-9c810de63604/scratchpad/pr-body.md
```

The body (written to the scratchpad first) lists: the design spec path; the three behaviour changes (no fused results; extraction sets are exactly the keyword hits; the summary excerpt is escaped); the settings renames (`HYBRID_FTS_MAX_RESULTS`, `HYBRID_HNSW_EF_SEARCH`, `HYBRID_FUSED_CACHE_TIMEOUT_SECONDS` → `SEARCH_*`, `HYBRID_RRF_K` removed) and the new settings with defaults; the worst-case waits (query embedding 10 s + 10 s gate, writer about 20 s); the production cache size; the measurement tables from Steps 3 and 4 and the iterative-scan decision; the screenshot follow-up (`Screenshot02_radis.png` and `Screenshot01_radis.png`, both copies, after the page ships). It ends with:

```
🤖 Generated with [Claude Code](https://claude.com/claude-code)

https://claude.ai/code/session_01XTntToXdCSFQYyhezJdYvc
```
