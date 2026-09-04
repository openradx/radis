# Extraction retrieval without the search cap — Design

**Date:** 2026-09-04
**Status:** Proposed
**Branch:** `extraction-unranked-retrieval` (off `fts-query-shape-performance`)
**Fixes:** openradx/radis#290 — extraction jobs silently process at most ~10,100 reports
**Depends on:** PR #292 (`2026-08-29-fts-query-shape-performance-design.md`) — see §6
**Scope:** `radis.pgsearch.providers.count` / `retrieve`, the extraction search preview,
the `EXTRACTION_MAXIMUM_REPORTS_COUNT` setting

## 1. Problem

Search and extraction share one retrieval path. `count()` and `retrieve()` in
`radis/pgsearch/providers.py` both call `_fuse_hybrid`, the same function
`search()` uses, so an extraction job sees exactly what a search page sees:
the top `HYBRID_FTS_MAX_RESULTS` (10,000) full-text hits fused with the top
`HYBRID_VECTOR_TOP_K` (100) vector hits.

Those caps exist for a reason. Search is interactive and must answer in about
a second, and ranking every match with `ts_rank` has no top-k shortcut, so
the candidate set is bounded before ranking (hybrid-search.md §7). But
extraction has no such need. It is a background job that may take hours, and
its user asked for *every* report matching the criteria, not the best 10,100.

The result is issue #290: a query matching 50,000 reports shows "10,100
reports will be retrieved" in the preview, passes the
`EXTRACTION_MAXIMUM_REPORTS_COUNT` (25,000) guard because the count is
already truncated, and the job processes 10,100 reports without any
indication that 39,900 were dropped.

## 2. Decision

Give extraction its own retrieval: **every report whose text matches the
boolean query, unranked, uncapped.** The 25,000 extraction limit becomes the
only ceiling, and it is enforced where it always was, in the form.

Concretely:

- `count(search)` counts the reports whose `search_vector` matches the
  tsquery under the filters. No `ts_rank`, no vector half, no RRF, no 10,000
  cut. When `search.limit` is a positive integer the count stops there, so the
  form and the live preview can ask "is it more than 25,000?" without
  counting a million rows on every keystroke.
- `retrieve(search)` streams the document ids of that same set in
  `report_id` order.
- Search (`search()`) is untouched. It keeps its caps and its ranking.

Ranking is dropped, not just the cap, because ranking is what makes the cap
necessary. Without `ORDER BY ts_rank` there is nothing to bound: the GIN
index yields the match set directly and a bounded count can stop early.

### What extraction gives up

The vector half. Today a report can enter an extraction job on semantic
similarity alone, with no lexical match. After this change it cannot: the
extraction set is the boolean match set. This is deliberate. Vector retrieval
is top-K by construction and has no "all matches" form, so keeping it means
keeping a cap. A user who wants semantic breadth writes a broader boolean
query (synonyms with `OR`), which the preview count makes easy to tune.

### Alternatives rejected

- **Raise `HYBRID_FTS_MAX_RESULTS` for extraction only.** Still a cap, still
  silent above it, and still pays the full `ts_rank` sort over every match.
- **Enforce the limit again in the worker** (refuse or fail the job when
  `retrieve()` yields more than 25,000). The form already refuses; the extra
  guard covers only the case where the corpus grows between form submit and
  job start. Judged not worth the code for now.
- **Make `count()` exact but keep `retrieve()` hybrid.** Fixes the number in
  the preview but not the job. Rejected because the two must describe the
  same set (the existing comment on `count()` says exactly this).

## 3. Changes

### 3.1 `radis/pgsearch/providers.py`

Add one queryset builder, next to `_fts_candidate_queryset`:

```python
def _boolean_match_queryset(search: Search):
    """Every index row whose text matches the boolean tsquery under the filters.

    The extraction set. Unranked and unbounded on purpose: with no ts_rank
    there is nothing to sort, so the GIN index yields the match set directly
    and a bounded count can stop early. Same single-table discipline as
    _fts_candidate_queryset -- everything here is a column on
    ReportSearchIndex.
    """
```

It reuses the existing pieces unchanged: `_build_query_string(search.query)`
for the tsquery (which already carries `NOT`), `_language_configs` for the
per-language `search_vector @@ tsquery` branches (a document matches only
under the config it was indexed with, same `match_q` as
`_fts_candidate_queryset`), and `_build_filter_query(search.filters)` for
group, language, modality, date, sex, age and description. Empty `configs`
(empty corpus) returns `ReportSearchIndex.objects.none()`, mirroring the
guard in `_fuse_hybrid`. No `.distinct()`, no annotation, no `ORDER BY`.

Then:

```python
def count(search: Search) -> int:
    qs = _boolean_match_queryset(search)
    if search.limit:
        qs = qs[: search.limit]
    return qs.count()

def retrieve(search: Search) -> Iterator[str]:
    return (
        _boolean_match_queryset(search)
        .order_by("report_id")
        .values_list("report__document_id", flat=True)
        .iterator(chunk_size=settings.EXTRACTION_TASK_BATCH_SIZE)
    )
```

`count` with a limit compiles to `SELECT COUNT(*) FROM (... LIMIT n)`, which
PostgreSQL can satisfy by stopping after `n` index matches. Without a limit
(`0` or `None`) it is the exact match count, which is what the `Search`
docstring promises for `offset=0, limit=0`.

`retrieve` joins `reports_report` for the document id. That join is one row
per index row over the primary key, so it neither duplicates rows nor needs
`DISTINCT`; it is the same shape as the existing `filter()` provider used by
subscriptions. Ordering by `report_id` makes task batching deterministic and
is index-backed (the one-to-one column carries a unique index). The queryset is evaluated
lazily; nothing is materialised in Python.

`_fuse_hybrid` loses two callers. Update its docstring ("Shared by
search(), retrieve() and count()" is no longer true) and delete the comment
on `count()` about semantic-only queries bypassing the guard, since the two
functions now share a queryset by construction.

### 3.2 Callers: which `Search.limit` they pass

| caller | today | after |
| --- | --- | --- |
| `ExtractionJobSearchForm.clean` (`radis/extractions/forms.py`) | `limit=0` | `limit=settings.EXTRACTION_MAXIMUM_REPORTS_COUNT + 1` |
| `extraction_search_preview` (`radis/extractions/views.py`) | `limit=0` | `limit=settings.EXTRACTION_MAXIMUM_REPORTS_COUNT + 1` |
| `process_extraction_job` (`radis/extractions/tasks.py`) | `limit=provider.max_results` (`None`) | unchanged |

Passing "limit plus one" is what lets the form distinguish "exactly 25,000,
allowed" from "more than 25,000, refused" with a bounded count. The
comparison in the form (`retrieval_count > EXTRACTION_MAXIMUM_REPORTS_COUNT`)
does not change. The error message stays as it is, except that the number it
prints is now "more than 25,000" rather than an exact figure (see §3.3).

The provider registration in `radis/pgsearch/apps.py` keeps
`max_results=None` for `ExtractionRetrievalProvider`: with no cap in the
provider there is nothing to declare, and the second guard in the form
(`provider.max_results and retrieval_count > provider.max_results`) stays
inert as today.

### 3.3 Preview and wizard wording

`radis/extractions/templates/extractions/_search_preview.html` and the
summary step (`extraction_job_wizard_summary.html`, which shows the stored
`retrieval_count`):

- The bounded count can only ever report up to 25,001. Render a count above
  `max_reports_limit` as "more than {{ max_reports_limit }} reports" instead
  of "25001 reports". The red warning line below it stays.
- Change "will be retrieved" to say what the number now is: reports whose
  text matches the query. Suggested: "**{{ count }} reports** match the query
  and will be extracted."
- Keep the "Preview Search Results" link. It opens the ranked hybrid search
  page, which is still the most useful way to eyeball the query. Its result
  count can differ from the preview number (it includes vector hits and is
  capped at 10,000 with "at least"); that is acceptable and is not
  reconciled here.

The form error message in `forms.py` prints `retrieval_count`; make it print
"more than {max}" when the count exceeds the limit, matching the preview.

### 3.4 `radis/extractions/site.py`

No signature change. Update the `ExtractionRetrievalProvider` docstring so
`count` and `retrieve` say what they now mean: the set of reports whose text
matches the boolean query, `count` honouring `Search.limit` as an upper
bound.

### 3.5 `EXTRACTION_MAXIMUM_REPORTS_COUNT` becomes an environment setting

Once the search cap no longer applies, this limit is the real ceiling on an
extraction job, and the right value depends on the deployment (corpus size,
LLM throughput, how long a job may run). In `radis/settings/base.py`:

```python
EXTRACTION_MAXIMUM_REPORTS_COUNT = env.int("EXTRACTION_MAXIMUM_REPORTS_COUNT", default=25000)
```

Same pattern as `PGSEARCH_BULK_INDEX_CHUNK_SIZE`. The default is the current
value, so existing deployments see no change.

Deliberately **not** added to `example.env`. It is an operator override, not
a setting every deployment should think about, so it follows the precedent
of the PostgreSQL parallelism variables: documented in CLAUDE.md under the
environment-variables section with a note that it is set in `.env` only to
override the default, and absent from `example.env`.

### 3.6 Subscriptions

Unaffected. `radis/subscriptions/tasks.py` uses only
`subscription_filter_provider.filter(filters)`. The
`SubscriptionRetrievalProvider` registered in `pgsearch/apps.py` wraps
`retrieve` but has no caller today; it simply inherits the new semantics.

## 4. Behaviour summary

| situation | today | after |
| --- | --- | --- |
| 50,000 lexical matches | preview says 10,100, job runs 10,100 silently | preview says "more than 25,000", form refuses |
| 20,000 lexical matches | preview says 10,100, job runs 10,100 silently | preview says 20,000, job runs 20,000 |
| 500 lexical matches | preview says 500 (+ up to 100 vector-only), job runs those | preview says 500, job runs 500 |
| 0 lexical, 40 vector-only | preview says 40, job runs 40 | preview says 0, nothing to extract |
| embedding service down | falls back to FTS-only silently | no embedding call at all |

## 5. Tests

New: `radis/pgsearch/tests/test_provider_extraction.py`

- `count` is exact above the search cap: override `HYBRID_FTS_MAX_RESULTS`
  to a small value (say 5), create more matching reports than that, assert
  `count` returns all of them and `search().total_count` still reports the
  cap. This is the regression test for #290.
- `retrieve` is complete and ordered: same setup, assert the yielded document
  ids equal the full matching set in `report_id` order.
- `count` honours `Search.limit`: with 10 matches and `limit=4`, returns 4;
  with `limit=0` and `limit=None`, returns 10.
- Filters and `NOT` apply: a report in another group, and a report matching
  a negated term, are excluded from both `count` and `retrieve`.
- Multi-language: reuse the pattern from
  `test_process_extraction_job_with_no_language_matches_each_documents_own_config`
  at the provider level, so an English and a German report are each matched
  only under their own config.
- No embedding call: patch `EmbeddingClient` and assert `count`/`retrieve`
  never construct it, even with `EMBEDDINGS_MODEL` set.

Changed: `radis/pgsearch/tests/test_provider_hybrid.py`

- `test_retrieve_returns_hybrid_ordered_document_ids`,
  `test_retrieve_falls_back_to_fts_on_embedding_error`,
  `test_retrieve_excludes_negated_term_from_vector_candidates`,
  `test_openai_rate_limit_error_in_retrieve_falls_back_to_fts`: these assert
  hybrid behaviour of `retrieve`. Rewrite them against `search()` where they
  test fusion or fallback, and drop the `retrieve` variants; the fallback
  paths are already covered for `search()` in the same file.
- `test_count_matches_retrieve_union_for_semantic_only_query`: inverts. The
  vector-only report is now in neither `count` nor `retrieve`; assert
  `count == 0` and `retrieve` is empty, and that the two still agree.

Unchanged: `test_providers.py::test_count_helper_matches_search_total` still
holds for small corpora (below the cap the sets coincide).

Extraction: `radis/extractions/tests/test_forms.py` gains one test that
overrides `EXTRACTION_MAXIMUM_REPORTS_COUNT` to a small value, creates one
more matching report than that, and asserts the form is invalid with the
"more than" message; and one that creates exactly the limit and asserts the
form is valid. `test_views.py` gains a preview test asserting the "more
than" rendering. `test_tasks.py` needs no change beyond running green.

## 6. Dependency on PR #292

The cheap single-table queryset in §3.1 exists because #292 denormalised
the filter columns onto `ReportSearchIndex`. Without it,
`_build_filter_query` produces `report__` traversals, and the boolean match
becomes a three-table join with `DISTINCT` again. That would still be
*correct* (the fix does not depend on the projection for correctness, only
for speed), so if #292 is reworked or delayed this design stands and only
the performance section of the implementation needs re-measuring.

The branch is cut from `fts-query-shape-performance` for that reason. If
#292 changes column names or the filter builder before merging, rebase.

## 7. Out of scope

- Worker-side enforcement of the extraction limit (rejected in §2).
- Reconciling the "Preview Search Results" page count with the extraction
  count (§3.3).
- Any change to `search()`, the search caps, or the RRF fusion.
