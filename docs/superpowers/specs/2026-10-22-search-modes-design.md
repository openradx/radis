# Search modes: full-text with an optional similarity re-rank, and semantic search

**Status:** Design, approved in conversation on 2026-10-22, awaiting review of this document
**Branch:** `search-modes` (from `main` at 8ed9ea3e)
**Supersedes:** the fusion parts of `docs/superpowers/specs/hybrid-search.md`: §3 steps 1–5,
§7.1–§7.4, §7.6–§7.8, §8.2, the query-path rows and the two non-policies of §9 ("degrades
to FTS-only", "query embeddings are not cached"), the query-path test rows of §10, §11.5 and
§11.7. Its ingestion and embedding pipeline (§4–§6, §7.5, §8.1, the worker rows of §9) stays
authoritative.
**Related:** `docs/superpowers/specs/2026-08-29-fts-query-shape-performance-design.md` (the
full-text candidate query this design keeps unchanged); the spike branch
`hybrid-fusion-spikes` (unmerged, head 529ddac9) and its notes in
`docs/superpowers/specs/2026-10-18-hybrid-ranking-strategies-spike.md` on that branch.

**Delivery:** this design ships in two pull requests. PR #372 (`search-modes`) delivers the
two modes. Everything about the description re-rank of full-text hits and the description
writer is deferred to the stacked PR `search-rerank`, wherever it appears: `SimilarityRerank`
and `FullTextSearch.rerank` (§3.1, §3.3), the description in full-text mode and the window
re-rank (§4.1 last sentence, §4.2, the degradation paragraphs of §4.3 — its query-path
timeout ships here, the window layer and description cache of §4.4, §4.5's rerank clause,
§5.3's comparison, the re-rank and writer parts of §6.1, §6.4 and §6.6, §6.5), the
`SEARCH_RERANK_*` and `SEARCH_DESCRIPTION_*` settings of §7, the matching items of §8, §10
and §13, and their tests in §11. In this PR the description field is the semantic query
only, and the inactive field of the search row is hidden and disabled (§6.2, §6.3).

## 1. Summary

Hybrid search fuses the full-text hits of a keyword query with the vector neighbours of the
same keywords, using reciprocal rank fusion (RRF). The fused first page mixes reports that
contain the keywords with reports that merely resemble them, the count is the size of a union
nobody asked for, and the vector arm is blind to negation, so "Pneumothorax" surfaces
"kein Pneumothorax" as readily as a positive finding.

This design replaces fusion with two modes the user picks on the search page:

- **Full-text** (default): exactly the keyword hits, highlighted, with an exact count up to
  the 10,000 cap ("more than" beyond it). An optional *description* of the wanted reports,
  typed by the user or written by the LLM from the keywords, re-orders the top N hits by a
  blend of their similarity to the description and their keyword rank (pure similarity order
  when the weight is 1). Hits beyond N follow in keyword order.
- **Semantic**: a description field takes the place of the keyword field and is the query.
  Results are the N reports most similar to it, in similarity order, with no highlighting.
  An optional minimum similarity trims the tail.

The provider interface models this as a typed request hierarchy with one entry point, the
way search engines expose one search endpoint whose request structure carries the retrieval
and ranking choice. Nothing is fused. The embedding pipeline, the single-table full-text
candidate query, the per-language text-search configurations, subscriptions and the
extraction query generator are unchanged.

## 2. Goals and non-goals

Goals:

1. A user who types keywords gets the keyword hits, nothing else, with highlighting and an
   exact count up to the cap, optionally re-ordered by meaning.
2. A user who describes a case gets the closest reports.
3. No result appears that the user cannot explain from the page.
4. Extraction jobs retrieve exactly the keyword hits, so the extraction count equals the
   search count.
5. The change ships as two pull requests (see Delivery), each reviewable in full.

Non-goals:

- Hybrid fusion in any form. A future "hybrid" would be a third request type whose fusion is a
  provider detail; the registry is shaped so that adding it changes no contract.
- Negation handling in semantic mode. Embeddings are polarity-blind (NevIR, EACL 2024;
  reproduced SIGIR 2025); "kein Pneumothorax" embeds next to "Pneumothorax". That needs
  index-time work and is a separate design.
- A cross-encoder or LLM re-ranker. The window re-rank blends the cosine similarity between
  the description and the report vectors with the keyword rank; nothing scores
  query–document pairs.
- The Search Lab, the strategy registry, the LLM judge, the benchmark command and the
  contrastive re-rank from the spike branch. None of it is ported.
- Any search API. `radis/reports/api` exposes no search endpoint; this design adds none.

## 3. Interface (`radis/search/site.py`)

### 3.1 Request types

`Search` becomes a frozen, keyword-only dataclass base, matching `SearchFilters` beside it.
Each mode is a subclass carrying only its own inputs. The re-rank is its own value because it
is a stage over the keyword hits, not a property of the query.

```python
@dataclass(frozen=True, kw_only=True)
class Search:
    """Inputs every mode shares. Offset and limit both 0 ask the provider for the most
    accurate total count it can give (extractions rely on this)."""
    filters: SearchFilters
    offset: int = 0
    limit: int | None = 10


@dataclass(frozen=True)
class SimilarityRerank:
    """Re-order the top SEARCH_RERANK_WINDOW keyword hits by the blended similarity and
    keyword score of §4.2."""
    text: str          # __post_init__ rejects blank text with ValueError


@dataclass(frozen=True, kw_only=True)
class FullTextSearch(Search):
    query: QueryNode                           # the parsed keyword query, never None
    rerank: SimilarityRerank | None = None


@dataclass(frozen=True, kw_only=True)
class SemanticSearch(Search):
    text: str                                  # __post_init__ rejects blank text
    min_similarity: float | None = None        # per-search override of the setting; 0..1


SearchRequest = FullTextSearch | SemanticSearch   # plain assignment (ruff UP040 under py312)
```

Why subclasses rather than a mode field: a semantic search without text or a full-text search
without a query cannot be constructed, no field means different things in different modes, and
a later `SemanticSearch` feature ("reports like this one") has a home that does not touch the
full-text type. Why keyword-only: a required subclass field may not follow the base's
defaulted `offset`/`limit` otherwise, and all thirteen existing `Search(...)` call sites
already pass keyword arguments. Nothing uses tuple behaviour (`_replace`, unpacking), so the
NamedTuple-to-dataclass change costs the class name at each site and nothing else.
`SearchFilters` stays a mutable dataclass, so requests are unhashable; the ranking cache keys
by a JSON fingerprint (§4.4), never by `hash(request)`.

### 3.2 Provider contract

```python
class SearchProvider(NamedTuple):
    name: str
    search: Callable[[SearchRequest], SearchResult]
    max_results: int           # paging cap of a full-text list (SEARCH_FTS_MAX_RESULTS)
    semantic: bool = False     # True when an embedding model is configured
```

One `search` function, dispatching on the request type. `semantic` is evaluated once at
registration (`settings.EMBEDDINGS_MODEL is not None`; the pgsearch registration passes it
explicitly) and handed by the view to the search form, which decides whether the mode select,
the description and the cutoff field exist (§6.4, §6.6). The default keeps the stub
providers in `radis/search/tests` constructible. The extraction registry keeps `count` and
`retrieve`, typed against `FullTextSearch`:

```python
class ExtractionRetrievalProvider(NamedTuple):
    name: str
    count: Callable[[FullTextSearch], int]
    retrieve: Callable[[FullTextSearch], Iterable[str]]
    max_results: int | None
```

Its three call sites (`radis/extractions/forms.py`, `tasks.py`, `views.py`) change the class
name, pass no `rerank`, and keep the offset-and-limit-zero count convention. Subscriptions keep
`filter(SearchFilters)` untouched.

### 3.3 Result types

```python
class SearchResult(NamedTuple):
    total_count: int
    total_relation: Literal["exact", "at_least", "approximately"]
    documents: list[ReportDocument]
    degraded: bool = False     # the semantic part could not be applied (§4.3, §5.5)


class ReportDocument(NamedTuple):
    relevance: float | None    # ts_rank; None in semantic mode
    ...                        # identity and metadata fields unchanged
    summary: str               # HTML-safe markup: escaped text plus <em> highlights (§4.6)
    similarity: float | None = None   # 1 − cosine distance when measured, else None
    score: float = 0.0                # the value that decided the position (§4.2)
```

`score` is the blended window score for a re-ranked hit and the `ts_rank` for every other
full-text hit (no re-rank requested, no embedding model, degraded, beyond the window, or not
embedded yet); in semantic mode it is the similarity. `cosine_distance` and `rrf_score` are
removed; nothing outside `radis/search` and `radis/pgsearch` reads them. The defaults keep
the stub providers in `radis/search/tests` constructible.

## 4. Full-text mode (`radis/pgsearch/providers.py`)

### 4.1 Retrieval

Unchanged: `_fts_candidate_queryset` matches the full boolean tsquery under each language
configuration present in the corpus, ranks with `ts_rank` under the document's own
configuration, orders by rank then report id, and stops at `SEARCH_FTS_MAX_RESULTS`
(10,000). The list is the result: `total_count` is its length, `total_relation` is
`"at_least"` when the cap was hit, else `"exact"`. The `ts_rank` values are kept per id (today
they are dropped after ordering) because the re-rank and the `score` field need them.

Removed: the vector arm, RRF, and the three negation-exclusion helpers
(`_collect_and_negations`, `_build_negative_query_string`, `_exclude_negations`), which only
existed to make vector candidates respect `NOT`. The tsquery carries `!term` itself.

### 4.2 Window re-rank

Runs when the request carries a `SimilarityRerank` and an embedding model is configured:

1. Embed `rerank.text` through `_embed_query_cached`. Its mechanics are unchanged (fingerprint
   of endpoint, model, params, instruction, dimension and text; failures never cached); its
   docstring loses the "FTS-only" sentence and the "Task 3 … in the meantime" history
   comment is deleted. The call runs with the query-path timeout of §4.3.
2. Take the first `SEARCH_RERANK_WINDOW` ids of the keyword list (default 1,000; the
   Azure AI Search default for the same stage). When the list is shorter, the window is the
   whole list.
3. Measure their exact cosine distance in one lookup on the unique `report_id` column
   (`report_id__in`, never `pk__in`: index-row ids differ from report ids), served by its
   B-tree index and never by the HNSW index:
   `ReportSearchIndex.objects.filter(report_id__in=ids).exclude(embedding__isnull=True)
   .annotate(distance=CosineDistance("embedding", vec)).values_list("report_id", "distance")`.
   This stays off the candidate query the SQL-shape tests guard.
4. Score each measured window hit with both signals min-max normalised over the measured
   window hits:

   ```
   score = w · norm(similarity) + (1 − w) · norm(ts_rank),   similarity = 1 − distance
   ```

   `w = SEARCH_RERANK_SEMANTIC_WEIGHT` (default 0.7; 1.0 is pure similarity order). A
   constant input normalises to 1.0 (one hit, or equal ranks). Ties break by `ts_rank` then
   report id.
5. Order the measured window hits by score. Every other hit follows in keyword order with
   `score = ts_rank`: the hits beyond the window, and the window hits that have no vector yet
   (`similarity = None`). A report that is not embedded is therefore never placed among the
   re-ranked block for a reason the page cannot show; the page says why it sits where it does
   (§6.6). After a model swap, when nothing is embedded, the order is the keyword order.

The scoring is a pure function in `radis/pgsearch/utils/ranking.py`,
`rerank_window(hits, similarity_by_id, window, weight) -> list[tuple[int, float]]` with
`hits` the `(report_id, ts_rank)` pairs in keyword order and `similarity_by_id` the measured
hits only, so unmeasured and measured hits are distinguishable. No database access, so it is
unit-tested on hand-built lists. It is the spike's convex blend and tail rule recombined; the
spike's weight sat on the keyword side, this one sits on similarity.

### 4.3 Degradation and the query-path timeout

If the embedding call fails (rate limited, unreachable, misconfigured), the search runs in
keyword order and `SearchResult.degraded` is `True`. The existing once-per-configuration
traceback throttle in `_embed_query_or_none` stays; its messages stop promising
"FTS-only results" and say what happened per caller ("full-text re-rank skipped",
"semantic search unavailable") while still naming `EMBEDDINGS_BASE_URL`, `EMBEDDINGS_MODEL`,
the exception class and the once-only traceback. A degraded window order is never cached;
the keyword ranking beneath it is (§4.4).

The wait for that failure is bounded. Today a query embedding uses
`EMBEDDINGS_REQUEST_TIMEOUT_SECONDS`, which inherits the LLM default of 60 s; a black-holed
endpoint would hold every described search for a minute per page. `EmbeddingClient` gains an
optional `timeout` keyword, like `AsyncChatClient`, and `_embed_query_or_none` passes
`EMBEDDINGS_QUERY_REQUEST_TIMEOUT_SECONDS` (default 10 s, §7). The write path keeps the long
timeout. The query path has no transient retries, so the worst case per search is the
10 s request plus the existing 10 s rate-limit gate budget.

A `FullTextSearch` with a `rerank` in a deployment without an embedding model embeds nothing
and runs in keyword order with `degraded = False`: the page never builds such a request, and
a programmatic caller of the provider gets the keyword list.

### 4.4 Ranking cache

The fused-union cache becomes a ranking cache with the same discipline and TTL
(`SEARCH_RANKING_CACHE_TIMEOUT_SECONDS`, default 300, 0 disables), in two layers so an
embedding outage never costs the keyword ranking, which is the expensive part and does not
depend on the embedding:

- **Keyword layer**, always cached: ordered ids, `ts_rank` per id, relation, the language
  configurations and the tsquery string, keyed by a fingerprint of schema version, the
  unparsed query, every filter field and `SEARCH_FTS_MAX_RESULTS`. A plain full-text search,
  the same search with a description, and an extraction over the same query share this entry.
- **Window layer**, cached only when the embedding succeeded: the re-ranked window ids with
  their scores and similarities, keyed additionally by the re-rank text,
  `SEARCH_RERANK_WINDOW`, `SEARCH_RERANK_SEMANTIC_WEIGHT` and the embedding configuration.
  The result list is the window layer followed by the rest of the keyword layer.
- **Semantic entries** (§5): ordered ids and similarities, cached unless degraded, keyed by
  schema, the text, every filter field, `SEARCH_HNSW_EF_SEARCH`,
  `SEARCH_SEMANTIC_ITERATIVE_SCAN` and the embedding configuration. The minimum similarity is
  not in any key: it is applied to the cached list (§5.4).

Keys are a prefix plus a SHA-256 hex digest (`pgsearch-ranking-`, schema bumped so entries
written by older code are missed, not misread); payloads are plain dicts. Page two onwards,
repeated searches, and the extraction `count` and `retrieve` reuse the layers. Reports
created or updated within the TTL join a cached query's results when it expires, as today.
The converse is closed in this design: hydration adds the group predicate
(`group_ids__contains=[filters.group]`, single-table, so the SQL-shape guards hold) and skips
ids that no longer qualify, so a report removed from the group drops off the page at once
while the cached count catches up at expiry.

Production uses Django's `DatabaseCache`, whose default `MAX_ENTRIES` of 300 culls a third of
the table by key order once full, that is at random for hashed keys. With ranking entries of
up to about 200 KB, the 900 s query-embedding cache and the 3,600 s description cache sharing
one table across three replicas, that cap would evict hot rankings under modest traffic.
`radis/settings/production.py` sets `OPTIONS: {"MAX_ENTRIES": 2000}`; the TTLs bound the table.

### 4.5 Count and retrieve

`count(request: FullTextSearch)` and `retrieve(request: FullTextSearch)` ignore `rerank`: an
extraction set has no order and a count does not change under a re-order. They read the
keyword layer only, so they never call the embedding service whatever a caller passes, and
they always share the plain full-text entry. Behaviour change to state in the PR: an
extraction job's report set is today the fused union, including vector-only neighbours of the
keywords; afterwards it is exactly the keyword hits.

### 4.6 Hydration and the summary

The page slice is hydrated as today: headline and `ts_rank` under the document's own
configuration, `select_related("report")`, the group predicate of §4.4, no `report__`
traversal besides the headline's `report__body`.

The summary is escaped. Today `ts_headline` returns raw report text with `<em>` markers and
the template renders it with `|safe`, so every full-text hit shows unescaped report text;
semantic mode would show the raw opening of every report. The fix, applied to both paths:

- `SearchHeadline` uses the markers `start_sel="\x02"` and `stop_sel="\x03"`, control
  characters no ingestion path produces (any string but NUL is legal PostgreSQL text, so this
  is a convention, not a guarantee, and the helper tolerates strays).
- `summary_markup(body, headline, max_words=30) -> SafeString` in
  `radis/pgsearch/utils/document_utils.py` replaces `summary_with_fallback`: take the
  headline, or the first 30 words of the body when the headline is empty; `escape()` it;
  replace balanced marker pairs only (`\x02…\x03`) with `<em>…</em>`; drop any remaining
  `\x02` or `\x03`; `mark_safe` last (string replacement returns a plain `str`). A literal
  `<em>` inside a report stays escaped; a stray control character costs at worst that
  character, never an unbalanced tag.
- `_result_document.html` drops `|safe`. A plain string from any other provider is then
  autoescaped by default, which is the house pattern for untrusted text.

`document_from_pgsearch_response` takes explicit `relevance`, `summary`, `similarity` and
`score` arguments instead of reading annotations off the row, which also removes the two
`type: ignore` comments on the hydration loop (pyright flags unnecessary ignores in CI).

## 5. Semantic mode (`radis/pgsearch/providers.py`)

### 5.1 Retrieval

Embed `request.text` through `_embed_query_cached` with the usual query instruction prefix and
the query-path timeout. Run today's vector query without the negation exclusion:
`ReportSearchIndex.objects.filter(filter_query).exclude(embedding__isnull=True)
.annotate(distance=CosineDistance("embedding", vec)).order_by("distance", "report_id")
.values_list("report_id", "distance")[:SEARCH_HNSW_EF_SEARCH]`.

The list is the result: `similarity = 1 − distance`, `score = similarity`, `total_count` is
the list length, `total_relation` is `"exact"`. Reports without an embedding yet are
invisible here, as in today's vector arm; the admin's pending-embedding count shows that
backlog.

### 5.2 Depth and the beam

One HNSW pass yields on the order of `hnsw.ef_search` rows corpus-wide (measured about
1.45 × ef_search, because an index element can carry several heap tuples), and the filters
then shrink them. The slice caps the list at exactly `SEARCH_HNSW_EF_SEARCH` (500 today),
which is the semantic depth; it must stay in the query, because without a LIMIT the planner
abandons the index for a sequential sort. The beam width remains a libpq connection option
validated to 1..1000 at boot. There is no separate depth setting. The page words the count
as a depth: "the N reports most similar to your description".

### 5.3 Topping up under filters

Every search is group-scoped, so a group holding five percent of the corpus would get about
25 of 500, and a rare modality or a narrow date range cuts deeper. PR #284 measured pgvector's
iterative scan and rejected it for the fusion, where it only bought a per-group quota the
fusion did not want. Semantic mode is the opposite case: the beam is the whole product.

With `SEARCH_SEMANTIC_ITERATIVE_SCAN` on (the default), the semantic query runs inside
`transaction.atomic()` after the literal statement `SET LOCAL hnsw.iterative_scan =
strict_order` (the value comes from a fixed mapping, never from a parameter: psycopg cannot
bind parameters in `SET`), so the scan resumes until the slice is full or pgvector's default
budget of 20,000 scanned tuples is spent. Strict order keeps the distance ordering the query
sorts on. `SET LOCAL` needs the enclosing `atomic()` (outside a transaction it is a no-op
with a warning) and lasts to the end of the outermost transaction: the search view runs
outside any transaction, so the setting ends with the semantic query's own block; a caller
inside an outer `atomic()`, such as a test, keeps it until that transaction ends. Per-search
`SET LOCAL` rather than a connection option keeps a plain connection at `off`, costs one
statement per semantic search, and leaves the full-text window measurement, an id-list
query, untouched.

The reference numbers from #284 were taken on the 1.7 M-report corpus at `ef_search = 1000`
and `LIMIT 1000`: strict order cost about 2.4 × on unfiltered queries (103 ms against 43 ms
median), and under synthetic filters the scan filled 238 of 1,000 rows at one-percent
selectivity and 24 of 1,000 at a tenth of a percent before the tuple budget ran out, at three
to six times the single-pass latency. §10 re-measures at the 500 default. That measurement
may only flip the default value of the setting (in `base.py`, `example.env` and the §7 row);
the code path, the statement and the §11 tests are the same either way, and the outcome is
recorded in the PR description.

### 5.4 Minimum similarity

A cutoff is standard (Qdrant `score_threshold`, Weaviate `distance`, Elasticsearch kNN
`similarity`, Azure `threshold`); every engine leaves the number to the application because
similarities are comparable only within one model and corpus. It removes the tail of a query
that matches nothing. It does not certify relevance at the top and does nothing about
negation.

- `SEARCH_SEMANTIC_MIN_SIMILARITY`: float in [0, 1] or unset. Default unset (off) until
  calibrated.
- `SemanticSearch.min_similarity` overrides the setting for one search; the page offers it
  as an optional field in semantic mode (§6.1, §6.2). An empty field means the setting
  applies; a value of 0 turns the cutoff off for that search.
- Applied as a truncation of the ordered list after retrieval, when the result is built from
  the (cached) ranking: cut at the first hit whose similarity is below the effective cutoff.
  Not a SQL predicate, because with iterative scan on a distance predicate would make
  pgvector keep scanning for rows that can no longer pass, up to its tuple budget.
- `total_count` is the truncated length; the count line names the cutoff when the effective
  value is above 0.

The spike dropped a distance cutoff (commit 529ddac9). That one was relative (best distance
plus a margin) and trimmed the vector arm before fusing it with the keyword list, so anything
inside the margin still entered the page as a vector-only report, and it needed a tuned
deployment constant. This one is absolute, there is no keyword list for a vector-only report
to intrude on, and it is off until a value is chosen with the override field.

### 5.5 Degradation and caller errors

If the embedding call fails, the result is empty with `degraded = True`; the page says
semantic search is unavailable right now. Nothing is cached. The wait is bounded by the
query-path timeout of §4.3. A `SemanticSearch` in a deployment without an embedding model is
a caller error and raises `ValueError`: the page never builds one, because the form has no
mode field without the capability.

### 5.6 Hydration

No tsquery, so no headline and no `ts_rank` annotation; `relevance = None`. The summary is
`summary_markup(body, "")`, the escaped opening of the report. The group predicate of §4.4
applies.

## 6. The search page (`radis/search/`)

### 6.1 Form fields

```python
query = forms.CharField(required=False, label=False, max_length=200)   # server-side cap, was widget-only
mode = forms.ChoiceField(required=False, choices=[("", "Full-text"), ("semantic", "Semantic")])
description = forms.CharField(required=False, label=False, max_length=1000)   # rendered by hand (§6.2)
min_similarity = forms.FloatField(required=False, min_value=0, max_value=1,
                                  widget=forms.NumberInput(attrs={"step": "0.001"}))
```

The description is the query in semantic mode and optional in full-text mode, where it
drives the re-rank of §4.2. In PR #372 it is only the semantic query: full-text mode ignores
it (see Delivery).

`SearchForm.__init__(data, *, semantic: bool = False)` deletes `mode`, `description` and
`min_similarity` when `semantic` is `False`; the view passes the provider's capability. The
empty mode value means full-text, so the default never appears in the URL: the page's
`x-ignore-empty-inputs` directive disables empty inputs at submit. `clean_description`
collapses whitespace. `min_similarity` is not prefilled; its placeholder shows the setting's
value when one is set ("default 0.300", three decimals like the similarities). `to_filters(group_id)` builds `SearchFilters` from
the cleaned data, so the view builds it in one place. The no-op `form_show_errors` access in
`__init__` is dropped. The deep link from the extraction preview (query and filter
parameters, no mode) keeps working because an absent mode is full-text.

The description travels in the URL like the keywords, so it appears in web access logs and
browser history; the user guide advises against typing patient identifiers into it.

### 6.2 Layout

The query row is one crispy layout object rendering `form_elements/query_input.html`. With
the capability, the mode select sits on the left, outside the input groups, and beside it
one row per mode, of which only the active mode's is shown:

```
Full-text:  [ Full-text ▾ ] [ keyword query                              ] [x] [🔍]
Semantic:   [ Semantic  ▾ ] [ Describe the reports you are looking for   ] [🔍]
```

The keyword row is an input group of the keyword input, the overlay clear button and a
submit button. The description row, the partial `form_elements/description_input.html`, is
an input group of the description textarea (two rows, at most 1,000 characters) and its own
submit button. Wording, verbatim: the keyword placeholder is "Input your search terms", the
description placeholder is "Describe the reports you are looking for", the accessible names
are "Search mode" for the select, "Search terms" for the keyword input, "Clear search terms"
for the clear button and "Search" for both submit buttons, and the cutoff field's label is
"Minimum similarity". Enter in the textarea submits the search (`@keydown.enter` with a `shiftKey` check that calls
`$el.form.requestSubmit()`, so the submit handlers still run; Alpine has no `.exact`
modifier); Shift+Enter inserts a newline. `min_similarity` sits at the bottom of the Filters
card, shown in semantic mode only and `:disabled="mode !== 'semantic'"` so it never rides
along in a full-text URL; "Reset filters" clears it like every other filter, and an empty
field means the setting applies. Without the capability the row is today's single input
group: the "Search" label, the keyword input, the clear button and the submit button. Where
the full-text description and the "Write from keywords" button go is decided by the stacked
PR that brings them (see Delivery).

### 6.3 Switching modes

Alpine state in the existing `SearchForm` component: `mode` bound with `x-model.fill` so a
reload of `?mode=semantic` keeps the selection (plain `x-model` writes the data value into the
element on init under the vendored Alpine 3.15). The component initialises `mode` to `''`
and reads nothing from the DOM unconditionally, because the page without the capability
(§6.6) has no select, textarea or cutoff field. The description textarea carries no
`x-model`; its value is read by the server from the form.

Each row is shown only in its own mode (`x-show`), and its fields are disabled in the other:
the keyword input and the clear button carry `:disabled="mode === 'semantic'"`, the
textarea `:disabled="mode !== 'semantic'"`, like the cutoff field. A disabled control is
never submitted, so the URL carries only the active mode's field and a result page never
shows the other mode's input. Hidden and disabled controls keep their values, so whatever
was typed in either field survives switching back and forth within the page; the next
search submits only the active one. The description row carries `x-cloak`, so it does not
flash before Alpine has applied the mode from the URL; the keyword row, the server-rendered
default, does not, so the page keeps a working full-text search without JavaScript.

The server trusts none of it. `SearchForm.clean()` counts only the active mode's field: in
semantic mode it drops the keyword field, a validation error on it included, and in
full-text mode the description, so a hand-made URL carrying the other field still searches
and the view never parses keywords in semantic mode; `min_similarity` is read only in
semantic mode. Semantic mode with an empty description renders a one-line notice above the info card
without calling the provider ("Semantic mode needs a description."). Full-text mode without
keywords shows the info card alone, as today, whatever else the URL carries.

### 6.4 The view

The provider check moves before the form: `ImproperlyConfigured` when no provider is
registered, then `SearchForm(request.GET, semantic=search_provider.semantic)`. Full-text:
`FullTextSearch(query=node, rerank=SimilarityRerank(description) if description else None,
filters=form.to_filters(group.pk), offset=..., limit=...)`; the "fixed query" notice stays.
Semantic: `SemanticSearch(text=description, min_similarity=..., filters=..., offset=...,
limit=...)`. The paging guard against `max_results` stays; a semantic list is at most 1,000
long. `page_sizes = [10, 25, 50]` is passed as the sibling list views do, so the page-size
selector the user guide already promises renders.

The context gains `mode`, `description`, `reranked` (a description was given and the result
is not degraded), the window size, the weight, the effective cutoff, `result.degraded` and
`page_sizes` for the templates.

### 6.5 The description endpoint and writer

`POST search/description/`, name `search_description`, in `radis/search/urls.py`. An async
function view following `chat_update_view`: `@require_POST`, then an inline
`if not request.htmx: raise SuspiciousOperation` as the first statement, so a non-HTMX
request gets 400. After `await request.auser()` an anonymous request gets an HTMX client
redirect to the login page (`HX-Redirect`; a plain 302 would make HTMX swap the login page
into the description row), and a user without an active group gets `PermissionDenied`, the
search page's own gate. The core
LLM client is not changed; the spike's sync twin is not ported.

Flow: bind the POST (HTMX includes the enclosing form's enabled inputs; the CSRF header
comes from the shared layout's `hx-headers` on `<body>`) to a small ORM-free form with the
same `query` (200) and `description` (1,000) limits; an invalid binding gets the failure
notice with no parse and no LLM call. Parse the query and render the positive terms with
`QueryParser.unparse_positive_terms` (today's `unparse_for_embedding`, renamed with its test;
`NOT` branches are dropped so the LLM does not describe the excluded finding). Blank or
`NOT`-only terms get their own notice ("Enter search keywords first; the description is
written from the positive terms.") without an LLM call. Otherwise call the writer.

Response: the description partial of §6.2 re-rendered, swapped `outerHTML` into
`#id_description`, plus the `<small id="description_notice">` as an out-of-band swap in
every response, empty on success. On success the textarea holds the new text, truncated to
1,000 characters. On failure it holds the submitted text unchanged and the notice reads "The
LLM could not write a description right now." Nothing in the view touches the ORM: the
partial is form-free, and the render runs inside one `sync_to_async` call because context
processors may query. The button is `type="button"` with `hx-post`, `hx-target`,
`hx-swap="outerHTML"`, `hx-disabled-elt="this"`, `hx-sync="this:drop"` and an indicator, so
repeated clicks never start parallel calls.

Writer: `radis/search/utils/description.py`, `async def write_description(terms: str) ->
str | None`. Ported from the spike's `query_expansion.py`: whitespace-collapsed input; cache
lookup (`cache.aget`) keyed by a hash of `LLM_BASE_URL`, the model spec, the system prompt and
the terms, prefix `search-description-`; `AsyncChatClient("query_generation",
timeout=SEARCH_DESCRIPTION_TIMEOUT).chat([system, user], max_completion_tokens=
SEARCH_DESCRIPTION_MAX_TOKENS, transient_retry_attempts=0, max_wait=
SEARCH_DESCRIPTION_TIMEOUT)` closed in `finally`; `openai.OpenAIError`, `RateLimited` and
`LLMResponseError` log a warning and return `None`; the answer is whitespace-collapsed and
stripped of surrounding ASCII double quotes (`strip('"')`), a blank answer counts as failure;
successes are cached for `SEARCH_DESCRIPTION_CACHE_TIMEOUT_SECONDS`, failures never. No
transient retries and a gate budget equal to the timeout, because a user is waiting on a
button: the worst case is about twice the timeout. The model is the existing
`query_generation` feature (`LLM_QUERY_GENERATION_MODEL` override), since both features
write search inputs; the writer's timeout, token cap, cache and prompt are its own settings.
The keywords are the user message and the system prompt is static, so an injected answer
reaches only the submitting user's textarea and the cache entry for those exact terms.

System prompt default (the spike's, verbatim): "You help search a hospital archive of
radiology reports. The user gives search keywords. Write the short passage (two to three
sentences) that a radiology report matching those keywords would typically contain in its
findings or impression: the same language as the keywords, the usual terminology and
abbreviations of radiology reports, no patient names, no headings and no explanation. Output
only the passage."

### 6.6 Results and the no-embeddings deployment

- Count line, full-text. Without a description, or degraded: "N reports found", "More than
  N reports found" when capped. Re-ranked (`reranked` is true) with `total_count` at most the
  window: "N reports found, re-ranked by your description"; above the window: "N reports
  found, the top W re-ranked by your description, the rest in keyword order", W from the
  context, never a literal. With no hits the line reads "0 reports found" without the
  re-rank clause, even when a description was given.
- Count line, semantic. "The N reports most similar to your description", plus "with
  similarity at least 0.xxx" when the effective cutoff is above 0. Zero hits: "No reports
  similar to your description" with the same cutoff clause, so the user sees the cutoff
  removed everything.
- Degraded. Full-text: a warning, "Your description could not be applied (embedding service
  unavailable); results are in keyword order. Search again to retry.", and no re-rank
  wording anywhere. Semantic: an error, "Semantic search is unavailable right now.", alone,
  with no count line, no empty-state alert and no pagination.
- Result header right column: "Full-text rank" when `relevance` is not `None`;
  "Similarity" when `similarity` is not `None`; "Re-rank score" when `reranked` and the hit
  has a similarity; for a window hit without a similarity while `reranked`, "Similarity: not
  available (report not embedded yet)". Tooltips explain each number: similarity as the
  cosine similarity between the description and the report, the re-rank score as
  "w × similarity + (1 − w) × keyword rank, each scaled 0–1 within the top W", with w and W
  from the context. The RRF line goes. `floatformat` on `None` renders an empty string, so
  the template tests for `None` explicitly.
- Summaries: highlighted in full-text mode, the report's opening in semantic mode, both
  escaped (§4.6).
- The info card branches on `form.fields.mode`. With the capability it explains the two
  modes, the description's two roles, the write button, the cutoff, that operators, quotes
  and `NOT` have no special meaning in a description, that semantic mode has no highlighting,
  and that newly imported reports are embedded in the background and rank by keywords only
  until then and do not appear in semantic mode. Without it, today's card. Both fix a
  pre-existing error: `-` does not exclude a term, `NOT` does. The note that an empty
  `EMBEDDINGS_MODEL` hides the semantic controls lives in the user and admin guides (§8).
- Without an embedding model (`semantic` is `False`): the form deletes the three fields, the
  query row renders the "Search" label instead of the select, the description row, the write
  button and the cutoff field are absent, and the page is today's full-text page without
  fusion.

## 7. Settings (`radis/settings/base.py`, `example.env`)

The `HYBRID_` family goes, since nothing is hybrid. Renames keep value and semantics; the old
names are documented nowhere and set in no known deployment, so there is no compatibility
shim. Every new numeric setting uses `_optional_env` and a range check, the existing idiom,
so a bad `.env` value is a boot error naming the variable. Every env-driven setting gets a
commented `example.env` entry with its default.

| Setting | Was | Default | Meaning |
|---|---|---|---|
| `SEARCH_FTS_MAX_RESULTS` | `HYBRID_FTS_MAX_RESULTS` | 10,000 (constant) | full-text candidate cap and paging cap |
| `SEARCH_HNSW_EF_SEARCH` | `HYBRID_HNSW_EF_SEARCH` | 500, 1..1000 | beam width, libpq `-c hnsw.ef_search`, and the semantic list depth |
| `SEARCH_RANKING_CACHE_TIMEOUT_SECONDS` | `HYBRID_FUSED_CACHE_TIMEOUT_SECONDS` | 300, 0 disables | ranking cache TTL, both layers and semantic entries |
| `SEARCH_RERANK_WINDOW` | new | 1,000, ≥ 1 | keyword hits re-ranked when a description is given |
| `SEARCH_RERANK_SEMANTIC_WEIGHT` | new | 0.7, 0..1 | weight of similarity in the window score |
| `SEARCH_SEMANTIC_ITERATIVE_SCAN` | new | on | `SET LOCAL hnsw.iterative_scan = strict_order` per semantic search |
| `SEARCH_SEMANTIC_MIN_SIMILARITY` | new | unset | similarity cutoff, 0..1; unset is off |
| `EMBEDDINGS_QUERY_REQUEST_TIMEOUT_SECONDS` | new | 10 s | request timeout of a query embedding (search path only; worst case with the gate budget 20 s) |
| `SEARCH_DESCRIPTION_SYSTEM_PROMPT` | new | §6.5 text | the writer's instruction |
| `SEARCH_DESCRIPTION_TIMEOUT` | new | 10 s | writer request timeout and gate budget (worst case about 20 s, no retries) |
| `SEARCH_DESCRIPTION_MAX_TOKENS` | new | 200 | writer answer cap |
| `SEARCH_DESCRIPTION_CACHE_TIMEOUT_SECONDS` | new | 3,600 | cached answers per terms, model and prompt |
| `HYBRID_RRF_K` | removed | | |

`radis/settings/production.py` adds `OPTIONS: {"MAX_ENTRIES": 2000}` to the database cache
(§4.4). Unchanged: all other `EMBEDDINGS_*` settings, including the query-embedding cache (it
now holds the embedded description), and the extraction `QUERY_GENERATION_*` group. Comments
that describe the fusion, argue against iterative scan, or promise an FTS-only fallback are
rewritten, including the rate-limit comment "falling back to FTS-only beats hanging"; the
comment claiming `production.py` extends the database `OPTIONS` is wrong (it sets the
password) and is fixed in passing. The `EMBEDDINGS_QUERY_INSTRUCTION` default still says
"Given a radiology search query"; it now prefixes descriptions, which are queries too, so the
text stays and its documentation says what is embedded.

## 8. Documentation

All of these describe fusion, RRF, a silent FTS-only fallback, or a page without a mode
select, and change in this PR (the AGENTS.md rule to keep docs in sync applies):

- `AGENTS.md` (`CLAUDE.md` and `GEMINI.md` are symlinks): overview, tech stack line, the two
  app descriptions, the search architecture bullets, the lint line (the gate is ruff, pyright
  and djlint, and ruff also selects `UP`), the per-feature model list (the writer uses
  `LLM_QUERY_GENERATION_MODEL`), the embeddings heading and settings block with the new
  settings, the troubleshooting section retitled and reworded around the degraded notices
  and the query-path timeout, the "chat and query generation run in web" line, and the
  removal of the "Search via API" example, which documents an endpoint that does not exist.
- `README.md` (two sentences) and `docs/index.md` (one).
- `docs/user-docs/user-guide.md`: the overview sentence (line 11), the search steps, the
  semantic search section, the query length sentence, the filters list (cutoff field), the
  results section (scores per mode, no highlighting in semantic mode; the page-size sentence
  becomes true with §6.4), the advice not to type patient identifiers into the description,
  and the note that semantic controls appear only when the administrator configured an
  embedding model.
- `docs/user-docs/admin-guide.md`: the embeddings section retitled with its two anchors
  updated, a search tuning subsection with every §7 setting and the cache `MAX_ENTRIES`, the
  degraded wording, the per-feature list, the upgrade step's setting groups, and the note that
  unembedded reports never appear in semantic mode and are listed after the re-ranked hits in
  full-text mode.
- `docs/dev-docs/architecture.md`: embeddings wording, the provider interface, the ranking
  paragraph (which also names a setting that no longer exists).
- `docs/dev-docs/contributing.md`: the embedding setup paragraph and the per-feature note.
- `example.env`: the embedding header comment, the instruction comment, the new entries.
- `radis/core/templates/core/home.html`: the hybrid wording, the typo, and "BM25", which
  pgsearch does not use.
- `radis/search/templates/search/_search_info.html` as in §6.6.
- `TODO.md`: the distance-threshold entry is resolved by §5.4 and removed.
- `docs/superpowers/specs/hybrid-search.md`: a superseded banner at the top naming this spec
  and the sections listed in the header; the file stays as history.
  `docs/superpowers/specs/2026-08-29-fts-query-shape-performance-design.md`: one line under
  its "Consumers" paragraph noting that `_fuse_hybrid` and `_exclude_negations` were removed by
  this design; otherwise history. The plans directory stays untouched.
- Wording in code: the `ReportSearchIndex` docstring, the embedding client's error message,
  comments and docstrings, the settings docstrings, a test name in `radis/core/tests`, and
  the cross references to `test_provider_hybrid.py` in `test_admin.py`,
  `test_query_embedding_cache.py` and `test_language_config.py`.
- Screenshots: `Screenshot02_radis.png` shows the search page and is retaken after the page
  ships, in full-text mode with a description, written to both `resources/screenshots` and
  `docs/assets/screenshots`; `Screenshot01_radis.png` photographs the home page copy and is
  retaken in the same session. Not a blocker for the PR; tracked as a follow-up.

## 9. Removals

Code: the vector arm inside `_fuse_hybrid_uncached`, `rrf_fuse` and `radis/pgsearch/utils/
fusion.py` (its `summary_with_fallback` becomes `summary_markup` in `document_utils.py`),
the three negation-exclusion helpers, `_FusedHybrid` and the fused cache functions (replaced
by the ranking cache), `HYBRID_RRF_K`, the `cosine_distance` and `rrf_score` fields, the two
hydration `type: ignore` comments, the `Search` NamedTuple.

Tests: the RRF tests in `test_fusion.py` (the summary tests move with the helper),
`test_negative_query_string.py`, `test_provider_hybrid.py` and `test_fused_cache.py` (both
rewritten as per-mode modules, §11), the four database tests of `test_query_embedding_cache.py`
(rewritten per mode; its two fingerprint unit tests stay), the vector-side negation test and
its cross references in `test_language_config.py`, and the duplicated cache-clearing fixture.

Not ported from the spike: the strategies registry, the Search Lab, the LLM judge and
benchmark, the contrastive re-rank, the `hybrid`/`strategy`/`expand` form fields, the sync
LLM client additions, the `judge` LLM feature.

## 10. Performance and staging measurements

Expected: full-text mode without a description costs what the full-text half costs today, with
no embedding call. With a description: one cached embedding call, one id-list query reading up
to 1,000 vectors (4 KB each), and the two cache layers for pages two onwards. Semantic mode:
one cached embedding call and one HNSW scan, about 100 ms unfiltered with iterative scan on
by the #284 numbers at depth 1,000.

Before the defaults are fixed, measured on the side stack (`radis_spike` on port 8503, sharing
the staging database, rebuilt from this branch): semantic latency with the scan on and off at
the 500 default, unfiltered and under the group and a modality filter, with `EXPLAIN
ANALYZE` confirming the HNSW index on the real corpus; the window measurement at 1,000; and
the similarity ranges of the spike's benchmark queries, present versus unmentioned, to pick a
first cutoff. Only ids, verdicts and numbers are recorded. No report text is read.

## 11. Testing

Lint gate: `uv run cli lint` runs ruff (E, F, I, DJ, UP), pyright in basic mode with
unnecessary-ignore reporting, and djlint; all three must be clean. `uv run cli test` and CI
select `radis.settings.test`, where `EMBEDDINGS_MODEL` is `None`; a bare `pytest` uses the
development settings and `.env`. Every semantic or re-rank test therefore sets the model
explicitly (`parse_model_spec("qwen3")`) and mocks `EmbeddingClient`, and every no-model
test sets `EMBEDDINGS_MODEL = None` explicitly, as the existing pgsearch tests do. Warnings
are errors; async tests carry `@pytest.mark.asyncio`.

Pure functions (`test_ranking.py`, `test_document_utils.py`):
- window order with the blended score; window hits without a vector follow the measured
  block in keyword order together with the hits beyond the window; constant inputs normalise
  to 1.0; a window of one; empty input; all unmeasured gives the keyword order.
- `summary_markup`: headline path and fallback path; `<script>` and `&` in a body come out
  escaped; a highlighted word next to `<` keeps intact `<em>`; a literal `<em>` in a report
  stays escaped; an unbalanced sentinel in the body or the headline yields no tag.

Provider, full-text (`test_provider_fulltext.py`, the three-report fixture):
- no description: no embedding call, keyword order, `score` equals `ts_rank`, today's
  contract (the existing `test_providers.py` cases stay verbatim with the class rename).
- with a description: order follows the blended score; `similarity` values; a hit without a
  vector keeps `similarity = None`, is not dropped, and follows the measured block; only the
  first N hits are measured when N is 1; the re-rank embeds exactly the description, never
  the keywords.
- embedding failure (client error, 429, `RateLimited`): keyword order, `degraded = True`, the
  window layer not cached, the keyword layer cached.
- a re-rank without an embedding model embeds nothing and logs no error.
- `count` and `retrieve` take `FullTextSearch`, return only keyword hits, make no embedding
  call even when the request carries a `rerank`, and `count` equals the search total.
- hydration skips a report removed from the group after the ranking was cached.
- `EmbeddingClient(timeout=...)` reaches the SDK client, and the query path passes the
  query-path timeout.

Provider, semantic (`test_provider_semantic.py`):
- distance order; a report with no keyword match is found by description; `relevance` is
  `None`; the summary is the escaped opening without `<em>`; `total_count` equals the list
  length and `total_relation` is `"exact"`.
- cutoff: truncation after retrieval; the per-search override beats the setting; 0 and
  unset leave the list intact; a cutoff nothing passes gives an empty result, not a fallback.
- iterative scan: `SHOW hnsw.iterative_scan` is `off` in a test that runs no semantic
  search (load pgvector first with a vector cast, as the existing GUC test does);
  `CaptureQueriesContext` shows `SET LOCAL hnsw.iterative_scan = strict_order` immediately
  before the `<=>` statement when the setting is on and no `SET` when it is off (a `SHOW`
  after the search cannot serve, because the provider's `atomic()` is a savepoint inside the
  test transaction and the setting lingers); `hnsw.ef_search` is the setting.
- embedding failure: empty, `degraded = True`, not cached; no model: `ValueError`.

Ranking cache (`test_ranking_cache.py`): identical searches and page two run no ranking
query; the keyword layer is shared by a plain search, a described search and an extraction
count; mode, description, window and weight participate in the window and semantic keys;
the cutoff does not; a degraded window order and a degraded semantic result are not cached;
zero disables.

SQL shape (`test_filter_query_equivalence.py`): the single-table and no-traversal guards stay;
new: the semantic scan is single-table and, with `SET LOCAL enable_seqscan = off` inside the
test's transaction, its plan shows `Index Scan using pgsearch_embedding_hnsw` (an
`Incremental Sort` above it is allowed; on a three-row table the planner otherwise prefers a
sequential scan); the window measurement is an id-list query; the hydration query carries
the group predicate and no join.

Search page (`test_forms.py`, `test_views.py`): the three fields exist only when the form is
built with `semantic=True`, and the view hands the patched provider's flag through (the tests
keep patching `radis.search.views.search_provider`); the query is rejected above 200
characters; the description collapses whitespace; the cutoff validates; full-text with a
description builds a `FullTextSearch` with a `SimilarityRerank`; semantic builds a
`SemanticSearch` with the override and ignores a posted query; the two mismatch notices;
semantic with a blank description calls no provider; the count line in all its forms (below
the window, above it, capped, semantic with and without a cutoff, zero hits); both degraded
states, with no re-rank wording and no "Re-rank score" line on a degraded page; the header
lines including the not-embedded-yet line; the info card with and without the capability; a
plain-string summary with `<b>` from a stub provider renders escaped and a marked-safe `<em>`
renders as a tag; `page_sizes` is in the context; `test_views_e2e.py` stays as is.

Endpoint and writer (`test_description_view.py`, `test_description.py`): anonymous gets a
redirect; non-HTMX gets 400; a user without an active group gets 403 and no LLM call; a query
over 200 characters gets the notice and no LLM call; blank or `NOT`-only keywords get the
"enter keywords" notice and no LLM call; the textarea comes back filled and the notice empty;
failure keeps the submitted text and shows the outage notice; the writer receives the positive
terms only (`pneumothorax NOT rechts` gives `pneumothorax`); one test deletes
`DJANGO_ALLOW_ASYNC_UNSAFE` from the environment before the request so a sync database call
in the view cannot hide behind the acceptance tests' escape hatch. Writer: cleaned passage,
prompt, model, timeout, zero retries and gate budget reaching the client, caching, uncached
failures (`APIConnectionError`, `RateLimited`, `LLMResponseError`, none of which sleep,
because retries are off), blank input and blank answer, quotes stripped, the client closed.
`radis/search/tests` gains the cache-clearing fixture `radis/pgsearch/tests` already has.

Other modules: `test_query_embedding_cache.py` keeps its two fingerprint unit tests and
rewrites the four database tests per mode (a repeated semantic search embeds once, a repeated
re-rank embeds once, a failure is not cached, different texts embed separately);
`test_embedding_failure_log_throttle.py` follows the per-caller wording while still asserting
the two setting names, "once" and the exception class; extractions keep their tests, plus
one that their requests never embed.

## 12. Rollout

Two pull requests (see Delivery): PR #372 against `main` from `search-modes`, then the
stacked PR `search-rerank` on top of it. The PR #372 description states the three behaviour
changes: no fused results, extraction sets are keyword hits only, the summary is escaped.
Settings renames and the new ones are listed for operators, with the worst-case waits of
§7 and the production cache `MAX_ENTRIES`. The side stack on port 8503 runs this branch for
the measurements in §10 and for trying the page on the real corpus before merge; the
iterative-scan default recorded there goes into the PR description.

## 13. Decisions

- One `search` entry point with typed request subclasses, not two callables: search engines
  model the mode in the request structure; the registry contract stays stable when a hybrid
  request type is added; typing still makes invalid requests unconstructible.
- Re-rank as a value, not a string field, because it is a stage with its own settings.
- Blend rather than pure similarity order, with the weight exposed: min-max normalised convex
  combination is the standard fusion of a lexical and a dense score over one candidate list,
  and `w = 1` gives pure order for whoever wants it.
- Window hits without a vector follow the re-ranked block in keyword order rather than being
  scored with similarity 0: their position is then explainable on the page, and a corpus
  with no embeddings yet ranks by keywords exactly.
- `count` and `retrieve` ignore `rerank`: a set and a count have no order, and extractions
  must never depend on the embedding service.
- Window 1,000, matching Azure's default text recall size and the spike's measured setting.
- Minimum similarity off by default with a per-search override, because the number is
  corpus-specific and the spike's deployment-constant cutoff is exactly what proved untunable.
- Iterative scan on for semantic mode, confirmed by measurement before merge, because the
  beam is the whole product there.
- Two cache layers, so an embedding outage never re-runs the keyword ranking and the
  keyword layer is shared by every caller of the same query.
- A 10 s query-path embedding timeout, because a user is waiting and the 60 s default is for
  the write path.
- The inactive field is hidden and disabled rather than shown locked, so a result page never
  shows the other mode's input and only the active field travels in the URL; values still
  survive switching modes within the page, and the server ignores keywords in semantic mode
  either way.
- Async endpoint on the existing async client, because that is what both interactive LLM
  calls on `main` do, and the spike's sync twin was untested.
- The writer reuses the `query_generation` model feature; its behaviour settings are its own.
- Escaping fixed in this PR although pre-existing, because the design touches exactly that
  path and semantic mode would widen the exposure to a whole mode.
- `HYBRID_` settings renamed without shims: undocumented, unset in every known deployment.
