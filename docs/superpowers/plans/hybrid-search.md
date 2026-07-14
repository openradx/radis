# Hybrid Search — Consolidated Implementation History

**Status:** All increments executed on `feat/hybrid-search`.
**Spec:** `docs/superpowers/specs/hybrid-search.md` (the single living spec these increments built).

This document replaces the seven per-increment implementation plans (removed
2026-07-14). The full task-by-task plan texts remain in git history — retrieve any
of them with `git show f15d15a7:docs/superpowers/plans/<name>.md` (the last commit
that contains all of them). Increments are listed in execution order.

## 1. Hybrid search core (planned 2026-05-28)

FTS + dense-vector retrieval fused via RRF: `ReportSearchIndex` gains an
`embedding vector(1024)` column with HNSW index, `EmbeddingClient`, deferred
embedding via Procrastinate subjobs on the dedicated `embeddings` queue and
worker, hybrid `search()`/`retrieve()` in the pgsearch provider, `embed_pending`
backfill command, admin actions. The bulk of the branch's early commits, through
the PR-cleanup pass (`b1226a51`).

## 2. Embedding client OpenAI-SDK migration (planned 2026-06-30)

Replaced the pluggable request/response backend abstraction (openai vs. Ollama
native `/api/embed`) with a single sync client over the `openai` SDK against one
OpenAI-compatible `/v1` endpoint; SDK retries disabled so RADIS owns retry
policy. Commits `f6fdad6b..79460288`, `531c2b2a`, `10d54c33`.

## 3. Embedding pipeline logging (planned 2026-06-30)

Structured INFO/WARNING logging across the pipeline: enqueue counts, per-task
start/finish with durations, admin-action audit lines (`admin.clear_embeddings:
user=… cleared N`), command summaries. What operators grep when the badge shows
something unexpected.

## 4. Embedding rate-limit gate (planned 2026-07-01)

Proactive sliding-window rate limiter (`EmbeddingRateLimitEvent` model, weight
accounting, search-priority spillover, reactive 429 handling, retry-after
parsing). Commits `9d8bc232..bdc25fd2`. **Superseded:** the proactive limiter was
replaced by reactive 429 backoff in increment 6; the never-applied migrations
were squashed away (`b198d3bf`).

## 5. Rate-limit generalization research (2026-07-01)

Research doc only (`cea9e5cb`): evaluated generalizing the pgsearch rate limiter
for LLM callers in core. Conclusion fed increment 6's design; RateLimited
auto-retry for batch tasks was deferred to a follow-up PR (port ADIT's pattern).

## 6. Shared 429 backoff → per-process gate (planned 2026-07-02)

First iteration: cross-process shared backoff state (`8a32d2a3..e4b47414`).
Replaced by a simpler per-process `RateLimitGate` (`67306dff`) — one gate per
provider (embedding vs. LLM), search queries bypass the pause with a short
budget and fall back to FTS-only. Transient (non-429) retries later unified on a
shared `radis.core.utils.rate_limit` helper, dropping the `stamina` dependency
(`4d8bdcbe`).

## 7. Backfill cancel + throughput knobs (planned 2026-07-02)

`cancel_backfill_embeddings()` helper, `embed_cancel` management command, admin
cancel-backfill button (queued backfill-priority subjobs only; cancelled jobs
are never revived), env-configurable `EMBEDDING_SUBJOB_SIZE` /
`EMBEDDING_BATCH_SIZE` / `EMBEDDINGS_WORKER_CONCURRENCY` with gentler defaults.
Commits `80bb9c37..16b42520`.

## 8. Admin badge per-subjob report counts (planned 2026-07-12)

The pipeline badge's queued/in-flight subjob counts now also show how many
reports those subjobs cover, summed DB-side via
`jsonb_array_length(args->'report_ids')`; zero counts render plain, `failed`
stays a bare subjob count. Changelist rendering tests added. Commits `8f1aad88`,
`263e4bad`.
