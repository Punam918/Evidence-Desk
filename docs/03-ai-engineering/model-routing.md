# Deterministic model routing and cost control

Router v1 is policy, not an LLM classifier. Inputs: current workspace privacy and budget settings, user preference (`auto` or logical key), requested intent (`ask`/`compare`), required schema capability, full context estimate, provider health and remaining deadline. Output: ordered maximum two eligible keys, reason codes, cost reservation and immutable config versions.

```text
eligible = enabled AND probe_passed AND allowed_by_workspace
eligible &= supports_required_schema AND context_can_fit_after_packing
eligible &= permitted_data_boundary AND breaker_not_open
if explicit_key: eligible = eligible intersect {explicit_key}; never switch
if compare: rank [paid_quality, paid_standard, local_standard]
else:       rank [paid_standard, local_standard]
if local_only: rank [local_standard]; never call a hosted rewrite/judge/summary
remove routes whose worst_case_attempt_cost exceeds remaining request/workspace budget
select first; retain at most one fallback satisfying every same constraint
no eligible -> 409 model_unavailable (or 429 budget_exceeded if budget was binding)
```

`paid_quality` starts disabled until comparative eval promotion. “Complexity” initially means explicitly selected two-revision compare, or query length >256 generation tokens; the latter only records a reason and does not automatically spend more. No speculative LLM call to decide which LLM to call. Local user preference with local endpoint down fails clearly; it does not send private text to paid fallback. Workspace policy change to local-only cancels queued/inflight paid attempts before next emission; already transmitted text cannot be recalled.

## Reservations and accounting

Workspace daily/monthly budgets default $2/$20 UTC; per-run cap $0.05; owner may lower, not exceed operator ceiling. Before each attempt lock budget account row and reserve worst-case `input_estimate*input_rate + max_output*output_rate` (per-million conversion), considering previous attempts. A request cap covers the sum of attempts, rewrite and summary calls. Internal cost uses integer micro-USD, rounded upward per reservation; actual billing reconciles separately. Local generation records zero API cost plus measured inference-seconds; infrastructure allocation is separate, not “free”.

At admission reserve first planned attempt; before retries reserve incremental cost in same locked account. Unique `(run_id, attempt_no)` prevents duplicate debits. Terminal usage release unused reserve, add measured charge. If worker dies or disconnect lacks usage, keep worst-case charge as estimated; settle via operator reconciliation, never silently zero it. Daily/monthly window rollover is based on attempt start UTC; money held across midnight remains attributed to that window. Idempotent replay of POST returns existing run and does not reserve again.

## Caching

Baseline caches only model weights and tenant-scoped embeddings of repeated normalized query text for 5 minutes. Key: workspace + embedding revision + normalization version + SHA256(query). No cross-tenant shared query cache or answer cache. Exact document embeddings may be reused only within workspace and identical embedding/chunk profile. Cache omission must not change authorization. Retrieval caching is deferred until corpus-generation invalidation is implemented; answer caching is optional and must also bind prompt/model/history/policy versions.

## Promotion and visibility

Router records candidate exclusion reasons, chosen key, fallback, cost estimate, actual/estimated usage and timings without raw query. Promote candidate only when paired eval has no >2-point drop in citation precision or abstention metrics, satisfies latency/cost gates, and passes adapter/security suite. Evaluate cost per successful task, not price/token alone. Evolution: offline labeled intent classifier if measured routing mistakes matter; no bandit traffic on private users by default.

Acceptance: concurrent budget race, midnight rollover, retry charge, mutable price version, local-only rewrite, explicit-model outage, fallback smaller context, no healthy models and cancellation before/after provider admission. See INT-BUDGET and CONTRACT-PROVIDER.

Cached usage pricing: normalize total input into uncached plus cached subset. The registry contains cached_input rate; paid_standard uses observed$0.10/million cached tokens. Other candidates conservatively use full input rate unless a verified cache tariff is configured; mark resulting cost estimated where actual provider discount is unknown. Never add cached tokens on top of an already inclusive input total. Cache writes or other provider fees must be disabled or explicitly represented before claiming exact cost.

## Graph recovery and uncertain transport

Disable SDK automatic retries. The router owns all definite pre-draft retry/fallback decisions. model_usage.execution_state marks each request issued before sending; an uncertain request outcome after transport failure or crash is not a definite provider failure. It terminalizes interrupted with estimated usage instead of automatic retry/fallback. A complete stored response can resume validation without generation. This rule applies before the first public delta as well as after partial output.


Dated promotional prices carry price_valid_until. After expiry, that route is ineligible until operator refreshes the price registry and reservation tests. Gemini3.8 Flash's observed promotion expires2026-12-31; no automatic continuation at obsolete rates. Google candidate requires paid tier and content policy verification before activation.

Implementation clarification: `model_usage.price_snapshot` stores decimal-string input/output/cache rates alongside the price version, so restart or registry changes cannot alter an admitted attempt's settlement. `inference_seconds` records measured transport elapsed time for hosted and local attempts; it is not a hardware cost estimate. Reservations occur immediately before an actual serialized attempt is issued; run admission alone creates no billable call. The provider coordinator rejects policy/source/cancel changes before transport and before subsequent emissions. Provider terminal usage can still be accounted under the fenced job lease after visibility is revoked. A response stored durably may be replayed to validation without another transport call. A zero-cost local attempt with unknown usage remains explicitly estimated.

T20 implemented and validated: `ai/router.py` separates route planning from retry decisions. It consumes currently authorized catalog availability, retains at most two keys, and permits only one retry of the original route plus one auto fallback. Ambiguous outcomes never retry, including before a public draft. A retry must leave at least10seconds for a first provider event after bounded jitter/Retry-After. `models/health.py` uses dispatcher-only shared state, a rolling last-five failure timestamp array and a90second token-fenced probe lease. An expired/stale probe cannot close a breaker. Ledger outcome observation is idempotent through a narrow dispatcher security-definer function; it exposes classification/model metadata only, never response content. Production retry/fallback orchestration now reads committed attempt state; actual packed budget quotes can select local before the initial manifest is frozen. `model-probe` provides a one-shot operator entry point for supervisor scheduling; see infra/workers/README.md for explicit hosted opt-in and conservative per-invocation/global UTC probe caps. All12 routing policy tests and the313-test full backend suite passed; see docs/10-testing/router-report.md.
