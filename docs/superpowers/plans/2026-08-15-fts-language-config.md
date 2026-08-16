# FTS Language-Configuration Fix Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make full-text matching use the same text-search configuration the document was indexed under, so a search with no language filter stops silently missing stemmed terms and stops failing to apply `NOT` exclusions.

**Architecture:** Indexing already uses the document's own language (`ReportSearchIndex.save()`). The query side resolves one config from the search filters, which is `simple` when no language filter is given — so stemmed lexemes never meet. The fix keeps a single bounded query but splits the full-text predicate into one branch per text-search configuration present, each restricted to the reports indexed under it, OR'd together. A specified `filters.language` still produces exactly one branch, so that path is unchanged.

**Tech Stack:** Django 6 ORM (`SearchQuery`, `SearchRank`, `SearchHeadline`, `Q`, `Case`/`When`), PostgreSQL full-text search with snowball configurations, pytest + pytest-django.

## Global Constraints

- Branch: `fix/fts-language-config`, forked from `main` at `471081f4`. Do not rebase or force-push.
- **The `simple` configuration does no stemming.** `code_to_language("")` returns `"simple"` (`radis/pgsearch/utils/language_utils.py:81-82`), which is why the no-filter path is broken. Language codes Postgres has no dictionary for also resolve to `simple` — that is correct and must keep working.
- A report's language is a required FK (`Report.language`), so every report belongs to exactly one configuration group.
- Behavior when `filters.language` IS set must not change: today it both restricts the queryset (`_build_filter_query`, `providers.py:117-118`) and picks the matching config, which is already correct.
- Single-language corpora must collapse to exactly one branch, i.e. the same SQL shape and cost as today.
- Do not change indexing. No migration, no reindex, no schema change.
- Lint with `uv run cli lint` before committing. **Never run `uv run cli format-code`** — it reformats the whole repository. Stage by explicit filename, never `git add -A`.
- Commit messages use conventional-commit style, with these trailers after a blank line:

```text
Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01WQ5JV4VntoXmqMjT7CHDVR
```

## Running the tests

The dev containers have **no live sync** with this worktree, so `docker exec … pytest` would test a stale snapshot. Run on the host, against the containerized Postgres:

```bash
cd /Users/kschlamp/workspace/adit-radis-workspace/projects/radis-fts-language
DATABASE_URL="postgres://postgres:postgres@host.docker.internal:5442/postgres" DJANGO_SETTINGS_MODULE=radis.settings.test uv run pytest radis/pgsearch/ radis/core/ radis/search/ -m "not acceptance" -q --no-header
```

Verified baseline at `471081f4`: **475 passed, 1 deselected, 0 failed**.

Two tests in `radis/pgsearch/tests/test_provider_hybrid.py` currently fail intermittently — `test_retrieve_excludes_negated_term_from_vector_candidates` and `test_search_excludes_negated_term_from_vector_candidates`. Measured rate: 1 failure in 10 runs of `radis/pgsearch/`. They are the symptom this plan fixes: `ReportFactory` draws a random language code, and the tests fail whenever the draw lands on a config that stems the negated term. **After Task 1 they must stop being order- or draw-dependent** — that is the acceptance signal, so run the suite several times, not once.

## File Structure

| File | Responsibility after this plan |
|---|---|
| `radis/pgsearch/providers.py` | Resolves a list of (config, language codes) instead of one config; builds one full-text branch per config for matching, ranking, negation-exclusion and highlighting |
| `radis/pgsearch/tests/test_language_config.py` | **New.** Regression tests derived from the observed Postgres behavior |
| `docs/dev-docs/architecture.md`, `AGENTS.md` | Document what an unset language filter means, and correct one stale claim |

---

### Task 1: Match, rank, exclude and highlight under each document's own configuration

**Files:**
- Modify: `radis/pgsearch/providers.py` — `_resolve_language` (line 67), `_exclude_negations` (153), `_FusedHybrid` (the NamedTuple `_fuse_hybrid` returns), `_fuse_hybrid` (320), `search()` (390)
- Create: `radis/pgsearch/tests/test_language_config.py`

**Interfaces:**
- Produces: `_language_configs(filters: SearchFilters) -> list[tuple[str, list[str]]]` — pairs of (text-search config, the language codes that resolve to it). Exactly one pair when `filters.language` is set.
- Consumes: `code_to_language` (`radis/pgsearch/utils/language_utils.py`), `radis.reports.models.Language`.
- Changes: `_exclude_negations(queryset, node, configs)` takes the config list instead of a single `language: str`. `_FusedHybrid` carries what `search()` needs to build per-config headlines — replace its `language: str` / `tsquery: SearchQuery` fields with `configs: list[tuple[str, list[str]]]` and `query_str: str`. `search()` is the only consumer (verified: `providers.py:392-398`).

- [ ] **Step 1: Write the failing regression tests**

Create `radis/pgsearch/tests/test_language_config.py`. These encode the behavior observed directly in Postgres: `to_tsvector('english','effusion')` is `'effus'` while `to_tsquery('simple','effusion')` is `'effusion'`, so the two never meet.

```python
import pytest
from django.contrib.auth.models import Group

from radis.pgsearch.providers import retrieve, search
from radis.reports.factories import LanguageFactory, ReportFactory
from radis.search.site import Search, SearchFilters
from radis.search.utils.query_parser import QueryParser

pytestmark = pytest.mark.django_db


def _make_search(query_str: str, group_id: int, language: str = "") -> Search:
    node, _ = QueryParser().parse(query_str)
    assert node is not None
    return Search(
        query=node,
        filters=SearchFilters(group=group_id, language=language),
        offset=0,
        limit=25,
    )


@pytest.fixture
def group(db):
    return Group.objects.create(name="radiology")


@pytest.fixture
def english_report(group):
    """A report whose language stems: 'effusion' is indexed as 'effus'."""
    report = ReportFactory.create(
        body="Findings: large pleural effusion on the left.",
        language=LanguageFactory.create(code="en"),
    )
    report.groups.add(group)
    return report


def test_a_stemmed_term_is_found_without_a_language_filter(group, english_report):
    """The bug this fixes: searching the exact word in the report found nothing,
    because the document was indexed under 'english' ('effus') while a filterless
    query was built under 'simple' ('effusion')."""
    result = search(_make_search("effusion", group.pk))

    assert [doc.document_id for doc in result.documents] == [english_report.document_id]


def test_a_negated_stemmed_term_is_excluded_without_a_language_filter(group, english_report):
    """The same mismatch made `NOT` a silent no-op: the exclusion never matched,
    so a report containing the negated term came back as a hit."""
    doc_ids = list(retrieve(_make_search("pleural AND NOT effusion", group.pk)))

    assert english_report.document_id not in doc_ids


def test_an_explicit_language_filter_still_works(group, english_report):
    """The already-correct path: filtering by language restricts the queryset AND
    picks the matching config, so both sides agree."""
    result = search(_make_search("effusion", group.pk, language="en"))

    assert [doc.document_id for doc in result.documents] == [english_report.document_id]


def test_documents_in_different_languages_are_each_matched_under_their_own_config(group):
    """A mixed corpus: each document must be matched under the config it was
    indexed with, not under one shared config."""
    english = ReportFactory.create(
        body="Findings: large pleural effusion.",
        language=LanguageFactory.create(code="en"),
    )
    english.groups.add(group)
    german = ReportFactory.create(
        body="Befund: Pleuraergüsse beidseits.",
        language=LanguageFactory.create(code="de"),
    )
    german.groups.add(group)

    english_hits = [doc.document_id for doc in search(_make_search("effusion", group.pk)).documents]
    german_hits = [doc.document_id for doc in search(_make_search("Pleuraerguss", group.pk)).documents]

    assert english.document_id in english_hits
    assert german.document_id in german_hits


def test_a_language_postgres_cannot_stem_still_matches(group):
    """Codes with no Postgres dictionary resolve to 'simple' on both sides, which
    already agreed — this must keep working after the change."""
    report = ReportFactory.create(
        body="Findings: pleural effusion.",
        language=LanguageFactory.create(code="th"),
    )
    report.groups.add(group)

    hits = [doc.document_id for doc in search(_make_search("effusion", group.pk)).documents]

    assert report.document_id in hits
```

`LanguageFactory` declares `django_get_or_create = ("code",)` (`radis/reports/factories.py:21-26`), so passing an explicit `code=` is idempotent and safe to repeat across fixtures — verified, no `get_or_create` dance needed.

Note that these tests pin the language explicitly rather than letting `code = factory.Faker("language_code")` draw one. That is the point: the existing negation tests fail intermittently *because* the draw is random, and a regression test for this bug has to make the stemming language deterministic.

- [ ] **Step 2: Run them and record the real failure output**

```bash
DATABASE_URL="postgres://postgres:postgres@host.docker.internal:5442/postgres" DJANGO_SETTINGS_MODULE=radis.settings.test uv run pytest radis/pgsearch/tests/test_language_config.py -q --no-header
```

Expected: the first two fail (no hit / not excluded); `test_an_explicit_language_filter_still_works` and the `simple`-language test pass already. Record exactly which failed and how — if any test fails for a reason other than the mismatch (a factory uniqueness error, say), fix the test before touching `providers.py`.

- [ ] **Step 3: Replace `_resolve_language` with `_language_configs`**

In `radis/pgsearch/providers.py`:

```python
def _language_configs(filters: SearchFilters) -> list[tuple[str, list[str]]]:
    """The text-search configurations to match under, each with the language codes
    indexed beneath it.

    Documents are indexed with their own language's configuration
    (``ReportSearchIndex.save()``), so a query built under a different one never
    meets them: 'english' stores "effusion" as 'effus' while 'simple' looks for
    'effusion'. With a language filter the queryset is already restricted to that
    language, so one configuration covers everything. Without one, every
    configuration present in the corpus needs its own branch — anything else
    silently drops the languages it does not match.
    """
    if filters.language:
        return [(code_to_language(filters.language), [filters.language])]

    codes_by_config: dict[str, list[str]] = {}
    for code in Language.objects.values_list("code", flat=True):
        codes_by_config.setdefault(code_to_language(code), []).append(code)
    # Deterministic order keeps generated SQL stable across requests.
    return sorted(codes_by_config.items())
```

Import `Language` from `radis.reports.models`.

- [ ] **Step 4: Match and rank per configuration in `_fuse_hybrid`**

Replace the single `tsquery` with one per configuration, OR the branches, and rank each document under its own configuration. A foreign configuration's `SearchRank` is not reliably ~0 — a term whose stem equals itself scores the same under every configuration — so the rank must be scoped by the same condition as the match, not taken as a maximum across configurations:

```python
    configs = _language_configs(search.filters)
    tsqueries = {config: SearchQuery(query_str, search_type="raw", config=config) for config, _ in configs}

    # A document matches only under the configuration it was indexed with.
    match_q = Q()
    for config, codes in configs:
        match_q |= Q(report__language__code__in=codes, search_vector=tsqueries[config])

    rank_expr = Case(
        *[
            When(
                report__language__code__in=codes,
                then=SearchRank(F("search_vector"), tsqueries[config]),
            )
            for config, codes in configs
        ],
        default=Value(0.0),
        output_field=FloatField(),
    )
```

Then use `.filter(match_q)` where the old code had `.filter(search_vector=tsquery)`, and `.annotate(rank=rank_expr)`.

Import `Case`, `When`, `Value` and `FloatField` from `django.db.models`.

- [ ] **Step 5: Exclude negations per configuration**

`_exclude_negations` must exclude a document when it matches a negated branch **under its own** configuration:

```python
def _exclude_negations(queryset, node: QueryNode, configs: list[tuple[str, list[str]]]):
    """Drop documents matching the query's top-level ``NOT`` branches from a
    vector-candidate queryset, so the FTS half's exclusions also bind the vector
    half. Each configuration is excluded separately: a document is only dropped
    when it matches the negation under the configuration it was indexed with, so
    the exclusion cannot silently no-op the way one shared configuration did."""
    negative_query_str = _build_negative_query_string(node)
    if not negative_query_str:
        return queryset
    for config, codes in configs:
        negative_tsquery = SearchQuery(negative_query_str, search_type="raw", config=config)
        queryset = queryset.exclude(
            Q(report__language__code__in=codes) & Q(search_vector=negative_tsquery)
        )
    return queryset
```

Update its call site in `_fuse_hybrid` to pass `configs`.

- [ ] **Step 6: Carry the configurations to `search()` and highlight per configuration**

Change `_FusedHybrid`'s `language: str` and `tsquery: SearchQuery` fields to `configs: list[tuple[str, list[str]]]` and `query_str: str`, and populate them in `_fuse_hybrid`'s return.

In `search()`, the headline and the page-slice rank both need the document's own configuration. Build them with `Case`/`When`:

```python
    configs = fused.configs
    tsqueries = {
        config: SearchQuery(fused.query_str, search_type="raw", config=config)
        for config, _ in configs
    }

    def _headline(config: str) -> SearchHeadline:
        return SearchHeadline(
            "report__body",
            tsqueries[config],
            config=config,
            start_sel="<em>",
            stop_sel="</em>",
            min_words=10,
            max_words=20,
            max_fragments=10,
        )

    if len(configs) == 1:
        config = configs[0][0]
        summary_expr = _headline(config)
        rank_expr = SearchRank(F("search_vector"), tsqueries[config])
    else:
        # Highlight each document with the configuration it was indexed under;
        # a headline built under another configuration silently highlights nothing.
        summary_expr = Case(
            *[
                When(report__language__code__in=codes, then=_headline(config))
                for config, codes in configs
            ],
            default=Value(""),
            output_field=TextField(),
        )
        rank_expr = Case(
            *[
                When(
                    report__language__code__in=codes,
                    then=SearchRank(F("search_vector"), tsqueries[config]),
                )
                for config, codes in configs
            ],
            default=Value(0.0),
            output_field=FloatField(),
        )
```

Then annotate `summary=summary_expr, rank=rank_expr`. `summary_with_fallback` already handles an empty headline, so the `default=Value("")` degrades to the body excerpt rather than to nothing.

Import `Case`, `When`, `Value`, `TextField` from `django.db.models`.

- [ ] **Step 7: Run the new tests and the full suite**

```bash
DATABASE_URL="postgres://postgres:postgres@host.docker.internal:5442/postgres" DJANGO_SETTINGS_MODULE=radis.settings.test uv run pytest radis/pgsearch/tests/test_language_config.py -q --no-header
```
Expected: all pass.

Then the full suite (baseline 475 passed, plus your new tests).

- [ ] **Step 8: Prove the intermittent failures are gone**

The two negation tests failed roughly 1 run in 10 before this change. Run the pgsearch suite **10 times** and confirm zero failures:

```bash
for i in $(seq 1 10); do DATABASE_URL="postgres://postgres:postgres@host.docker.internal:5442/postgres" DJANGO_SETTINGS_MODULE=radis.settings.test uv run pytest radis/pgsearch/ -m "not acceptance" -q --no-header 2>&1 | tail -1; done
```

Paste all ten result lines into your report. This is the acceptance signal for the whole task — a single green run proves nothing about a 10% failure rate.

- [ ] **Step 9: Lint and commit**

```bash
uv run cli lint
git add radis/pgsearch/providers.py radis/pgsearch/tests/test_language_config.py
git commit -m "fix(pgsearch): match full text under each document's own language config

Documents are indexed with their own language's text-search configuration, but
the query side resolved a single configuration from the search filters — 'simple'
when no language filter was given. Stemmed lexemes then never met: an English
report storing 'effus' was invisible to a filterless search for 'effusion', and a
NOT branch silently failed to exclude, returning reports the user excluded.

The full-text predicate is now one branch per configuration present, each
restricted to the reports indexed under it, for matching, ranking, negation and
highlighting alike. A specified language filter still yields exactly one branch,
so that path is unchanged, as is a single-language corpus."
```

---

### Task 2: Document what an unset language filter means, and correct one stale claim

**Files:**
- Modify: `docs/dev-docs/architecture.md` (the "Search Architecture" section)
- Modify: `AGENTS.md` (the "Hybrid Search Returns Only Full-Text Results" troubleshooting entry)

- [ ] **Step 1: Document the semantics in `architecture.md`**

Add to the Search Architecture section, matching the surrounding voice:

```markdown
**Text-search configurations**: reports are indexed with the PostgreSQL configuration for their own language, so stemming matches the text — an English report stores "effusion" as `effus`. Queries follow the same rule: with a language filter the search is restricted to that language and built under its configuration; without one it is matched under every configuration present, one branch per configuration, so a filterless search still finds stemmed terms in every language rather than only the ones a shared configuration happens to agree with. Languages PostgreSQL has no dictionary for fall back to `simple`, which does no stemming and therefore matches literally.
```

- [ ] **Step 2: Correct the stale throttling claim in `AGENTS.md`**

The troubleshooting entry added for the inherited-endpoint failure says the traceback "is not throttled, so the same traceback repeats per request". That stopped being true when the throttle landed: `_embed_query_or_none` now logs the full traceback once per `(EMBEDDINGS_BASE_URL, model)` configuration and a single-line WARNING thereafter. Rewrite that clause to describe the current behavior — an operator who sees one traceback followed by repeating WARNINGs should recognise it, and one who sees a traceback per request should know that is not what this code does.

- [ ] **Step 3: Verify and commit**

```bash
uv run --group docs mkdocs build --strict
uv run cli lint
git add docs/dev-docs/architecture.md AGENTS.md
git commit -m "docs: explain per-language text-search configs and fix a stale throttling claim"
```

---

## Self-Review

**Spec coverage.** Task 1 fixes matching, ranking, negation-exclusion and highlighting — the four places the resolved configuration was used. Task 2 documents the resulting semantics and corrects the one claim this branch's predecessor left behind.

**Deliberately not in scope.** Adding an "All languages" choice to the search form (`radis/search/forms.py:38` omits the empty option, so the UI always submits a language) — that is a product decision, and after this fix it becomes safe to make, but it is not required by the fix. Also out of scope: caching `_language_configs`. It is one indexed read of a small table per search; measure before adding a cache whose invalidation would then need thinking about.

**Type consistency.** `_language_configs` returns `list[tuple[str, list[str]]]` everywhere: produced in Task 1 Step 3, consumed by `_fuse_hybrid` (Steps 4-5), carried on `_FusedHybrid` and consumed by `search()` (Step 6). `_exclude_negations`'s third parameter changes type from `str` to that list — it has exactly one call site.
