# Rate-limit gate generalization research

## Context

The [design doc](2026-07-01-embedding-rate-limit-gate-design.md) and
[implementation plan](../plans/2026-07-01-embedding-rate-limit-gate.md) for the
embedding rate-limit gate confirmed empirically that the internal embedding
gateway enforces a genuine sliding-window rate limit: 60
request-equivalents per trailing 60-second period, where each admitted request
carries its own independent 60-second expiry timer counted from send time —
not a fixed clock-aligned window, not tied to completion, and not a continuous
token-bucket-style refill.

The `EmbeddingRateLimitEvent` ledger and `rate_limiter.py` gate were built to
match that exact model. This doc records follow-up research into whether that
model is representative of LLM/embedding API rate limiting generally, or
specific to this one gateway — i.e., how much the gate's design would
generalize if ever pointed at a different provider.

## The FastAPI/slowapi/limits lead

The captured 429 body, `{"detail": "Limit 60/min exceeded. Wait Xs."}`, uses
the `"detail"` key that is FastAPI/Starlette's `HTTPException` default,
strongly suggesting a FastAPI-based gateway.

`slowapi` (github.com/laurentS/slowapi) is a thin FastAPI/Starlette wrapper
around the `limits` package (github.com/alisaifee/limits), which implements
three interchangeable strategies: `fixed-window`, `moving-window`, and
`sliding-window-counter`. The `moving-window` strategy is exactly a genuine
per-timestamp sliding window log: it stores every request's timestamp and
admits a new request only if the *n*-th oldest recorded timestamp (n = limit)
is either absent or older than the window duration; each entry then expires
independently, 60s after it was recorded. This matches the observed gateway
behavior precisely, and matches the ledger design in
`EmbeddingRateLimitEvent`/`rate_limiter.py` structurally (append-only log,
opportunistic pruning of expired rows, per-row independent expiry).

Two caveats worth flagging:

- `limits`'/`slowapi`'s/`flask-limiter`'s **default** strategy is
  `fixed-window`, not `moving-window` — so if this gateway is built on
  `limits`, the moving-window strategy was a deliberate, non-default
  configuration choice, not something to assume by default for other
  FastAPI-based gateways.
- slowapi's built-in exceeded-handler by default emits
  `{"error": "Rate limit exceeded: ..."}`, not `{"detail": ...}`. The observed
  `{"detail": "Limit 60/min exceeded. Wait Xs."}` shape looks more like a
  hand-rolled `HTTPException(429, detail=...)` — either a custom wrapper
  around `limits`'s `MovingWindowRateLimiter` directly, or a customized
  slowapi handler. Either way, it's evidence of a FastAPI app deliberately
  using `limits`'s moving-window (true sliding window) implementation, just
  not proof of exactly which layer emits the message.

## Provider/gateway comparison

| System | Algorithm (best evidence) | Confidence / source |
|---|---|---|
| Anthropic API | **Token bucket**, continuously replenished | Official docs, explicit: "The API uses the token bucket algorithm... capacity is continuously replenished... rather than being reset at fixed intervals" ([platform.claude.com/docs/en/api/rate-limits](https://platform.claude.com/docs/en/api/rate-limits)) |
| OpenAI API | Rolling/continuous window; described community-side as token-bucket-like via `x-ratelimit-reset-requests`/`x-ratelimit-remaining-requests` headers, but exact algorithm not formally documented | Not officially specified; community reverse-engineering only |
| Azure OpenAI | "v2" tiers reportedly use token bucket; classic tiers evaluated in ~1–10s sub-intervals (sliding-window-ish) | Microsoft Learn docs + community Q&A; partially official, partially informal |
| AWS Bedrock | Token/leaky bucket on the provider side; AWS's own guidance recommends client-side token-bucket or sliding-window-tracker patterns to avoid throttling | AWS blog/docs; official for provider side, informal/advisory for client-side patterns |
| Google Vertex AI | Not determined — no clear public documentation of the exact algorithm found | Gap; not researched further within scope |
| LiteLLM proxy | In-memory counter incremented per request, periodically synced to Redis (~10ms interval); resembles a windowed counter, not a per-timestamp log | LiteLLM docs/release notes; moderate confidence |
| Kong (rate-limiting-advanced) | "Sliding window" is actually a weighted current+previous fixed-window counter (a sliding-window *counter* approximation), not a true timestamp log | Kong official docs |
| nginx `limit_req` | Leaky bucket (paces/smooths output at a fixed rate) | Official nginx docs |
| Envoy | Local: token bucket (no coordination); Global: delegates to an external gRPC rate-limit service, typically Redis-backed | Official Envoy docs |
| `limits`/`slowapi` (`moving-window`) | **True per-timestamp sliding window log** — matches this gateway's observed behavior | Library source/docs; high confidence |

## Assessment

A genuine per-timestamp sliding window (each admitted request independently
expiring) is the **least common** choice among production LLM rate limiters
surveyed here. Major hosted providers favor token bucket (Anthropic states
this explicitly; others imply similar continuous-refill behavior) because
it's cheap to implement — one counter per key, not a growing timestamp log —
and smooths bursts gracefully. Gateways like Kong default to sliding-window
*counter* approximations for the same memory-efficiency reason. True
sliding-window logs mainly show up in libraries that offer them as an
explicit opt-in (like `limits`'s `moving-window` strategy), precisely because
storing N timestamps per key is more memory/storage-intensive than one or two
counters — a tradeoff cited repeatedly across the sources above.

**Conclusion for this gate's generalizability:** the design correctly matches
*this* gateway's genuinely unusual (for the ecosystem) sliding-window
behavior, most plausibly because it's a custom/internal FastAPI gateway built
on the `limits` package's `moving-window` strategy. It should **not** be
assumed to generalize to other providers without re-verification.

## Practical implication if pointed at a different provider/gateway in future

Before reusing this gate's exact per-timestamp-expiry model against a new
target:

- Check whether the target advertises token-bucket refill (Anthropic does
  explicitly) — the gate's discrete per-request expiry math would be overly
  conservative or simply wrong against a continuous-refill backend.
- Check the semantics of any reset-timestamp response header: a single
  shared reset time for the whole limit suggests a window/bucket model,
  not independent per-request expiry.
- If pointing at another self-hosted FastAPI/`limits`-based gateway, check
  its configured strategy explicitly — `fixed-window` is the library
  default, `moving-window` (true sliding window) is an opt-in choice, and
  `sliding-window-counter` is a third, different approximation.
- Re-run the same kind of empirical two-wave timing test used against this
  gateway before trusting any new target matches this model; don't infer
  the algorithm from documentation alone unless it's as explicit as
  Anthropic's.
