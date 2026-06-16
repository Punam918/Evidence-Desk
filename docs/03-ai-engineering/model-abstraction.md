# Provider abstraction and serving

## Required adapters and capability negotiation

Core paid adapter: OpenAI Responses, current `gpt-6-luna` default candidate (reasoning none subject to probe; alias drift recorded). Core open adapter: Ollama native chat with Qwen3 8B non-thinking, pinned model digest. GPU alternative: vLLM chat-completions endpoint with an explicit Qwen template. Later adapters: Anthropic Messages and Google generateContent. [Research](../00-research/ecosystem-and-decisions.md) explains choices; the [model registry](../../contracts/config/models.yaml) is normative for enabled status and application limits.

| Adapter | Request mapping | Streaming and usage mapping | Structured output / tooling |
|---|---|---|---|
| OpenAI | `responses.create`, `input`, `text.format`, `max_output_tokens`, `store=false` | `response.output_text.delta`; completed usage; refusal and incomplete are distinct | Compile supported schema subset; API tool-call IDs remain provider IDs mapped to local execution IDs |
| Anthropic | `messages`, separate system, `max_tokens`, `output_config.format` on compatible model | content blocks and message delta; merge cumulative totals without double counting | Validate selected model supports schema/tool controls; thinking blocks never user-visible or logged |
| Gemini | `contents`, `systemInstruction`, generation config | candidate parts; finish/block reason; usageMetadata incl thinking | Provider schema subset; safety-blocked is refusal; empty candidate is failure |
| Ollama | `/api/chat`, messages, format schema, `stream=true`, non-thinking option verified in probe | NDJSON message content, final eval/prompt counts | Schema is still locally validated; tooling disabled for core local route; fixed workflow needs no native tool call |
| vLLM | `/v1/chat/completions`, explicit served model name/template | choice deltas, usage when configured; detect interrupted finish | No assumption of Responses parity; parser/template/options tested as a combination |

See official [OpenAI schema](https://developers.openai.com/api/docs/guides/structured-outputs), [Claude schema](https://platform.claude.com/docs/en/build-with-claude/structured-outputs), [Gemini schema](https://ai.google.dev/gemini-api/docs/structured-output), [Ollama compatibility](https://docs.ollama.com/api/openai-compatibility), [vLLM server](https://docs.vllm.ai/en/latest/serving/online_serving/openai_compatible_server/) docs. These document features, not successful project integration.

`ModelConfig` includes logical key, adapter, provider model ID, optional immutable revision/digest, endpoint alias, tokenizer revision/count method, application context cap, max output, supported role/schema/tool modes, privacy class, retry codes, timeout, prices and price timestamp. Secrets are environment/file references outside this registry. An alias without immutable provider snapshot is marked mutable; report resolved returned version and probe date. No `latest` model tag in a release manifest.

## Token and context rules

For each attempted model use its tokenizer/template or provider count endpoint on the **complete serialized request**, including schemas and role delimiters. Let `C=min(provider_limit, configured_limit)`, `O=1024` total output tokens (including reasoning if applicable), `S=max(256, ceil(0.05*C))`. Require `input_tokens + O + S <= C`. Default C=16384 paid, 8192 local; evidence ≤3072, history ≤2048, current query ≤512 model tokens, instructions/schema ≤2048. Actual full-request count governs, not the sum of approximate components. When over budget: remove oldest history, then lowest-ranked whole evidence blocks; never truncate policy or current query silently. Oversized query returns 422. If required source pair cannot fit, compare bounded sections or return a request to narrow scope.

Exact count unavailable: mark `estimated=true`, reserve conservative tokenizer estimate plus 20%; retain above slack; overflow 400 triggers one repack before any draft within attempt/deadline cap. A count estimate is never described as exact. BGE/reranker tokens differ from LLM tokens. Repack for fallback model; token counts cannot be reused across providers.

## Retry and error policy

Connection timeout is3s; hosted providers use a10s no-first-event timeout,10s idle-stream timeout and60s total run deadline. The local CPU Ollama adapter allows45s for its first stream event,10s idle gaps and85s total transport time; the durable run deadline is90s. This measured allowance covers the pinned CPU Qwen model's slow first full context evaluation while retaining a bounded terminal outcome. It does not retry or extend a partially issued answer. For retryable 429/502/503/connect reset before draft, at most one retry same provider with full jitter `uniform(0,min(4,0.5*2^attempt))` seconds; honor Retry-After if within deadline. Then one eligible fallback only in auto mode, maximum three billable attempts total. 401/403/invalid schema are configuration failures; no user-content retries. Refusal completes refused, never routes to evade refusal. After first draft, failure terminalizes interrupted; no splice of different models. Unknown usage reserves worst-case until reconciliation/expiry and is displayed estimated.

Circuit breaker per endpoint/model: after 5 transient failures in rolling 60s, open 30s; one half-open probe; success closes. Shared state in DB, not per API process. Probe every 5min for enabled endpoint; discovery access is not a quality gate. Warm model readiness requires schema, streaming, cancellation and usage fixtures. No runtime downloading untrusted model code (`trust_remote_code=false`).

## Hardware and open-weight deployment

Qwen3-8B has 8.2B parameters per [model card](https://huggingface.co/Qwen/Qwen3-8B). Raw weights roughly 16.4 GB at 16-bit or 4.1 GB at 4-bit before quantization overhead; these are arithmetic lower bounds, not minimum system RAM. Target local Q4 profile: 16 GiB machine, 8192 context, concurrency 1. Expect slower generation; benchmark before publishing latency. A 24 GiB GPU is a test candidate, not a guaranteed fit for every dtype/context/batch. GPU KV cache, activations, runtime overhead and concurrency must be measured. vLLM max_model_len=8192, max_num_seqs=2 initially, adjust after memory testing. Quantized and full-precision results are separate benchmark entries.

CPU-only development uses CPU embeddings/reranking plus hosted generation, or Ollama at reduced concurrency. The public 16 GiB demo host does not also host an 8B generator. An optional private inference endpoint uses TLS/auth and operator allowlisting; no user-controlled base URL. External cloud GPU is not “local-only”; only operator-designated trusted private endpoints qualify for that policy.

Acceptance: identical adapter contract fixtures for deltas, multibyte characters, usage duplication, schema refusal, empty completion, cancellation, timeout, provider error after draft, input overflow, tool argument fragmentation, and missing terminal event. A provider name is shown enabled only after these pass.

## Implemented adapter boundary (2026-10-11)

The native Responses adapter uses the installed OpenAI3.28 SDK (which uses httpx2), fixes its origin to `https://api.openai.com/v1`, disables redirects/environment proxies and SDK retries, and sets `store=false`/`truncation=disabled`. The normalized terminal retains inclusive token totals and cache/reasoning subsets. The application validates the answer again with Pydantic, including conditions the provider schema cannot express. Model tool output is rejected because this workflow does not request native tools.

The default preflight is a full serialized request tokenizer estimate plus20%, explicitly marked estimated. The adapter also exposes the native `responses.input_tokens.count` operation; it sends content and must only be called after the same hosted-data authorization as generation. It is not invoked as an unguarded API preflight. Scripted conformance is separate from live readiness and quality promotion.

A gateway5xx or connection interruption is classified as ambiguous, even before a draft. Only definite rejection (such as429) is eligible for a router-owned retry. This clarifies the older general retry list above in favor of the no-uncertain-replay invariant. Retry-After is preserved; the future router must decline a delay outside the remaining deadline rather than shorten it.

T21 adds a server-owned `output_kind` (`answer`, `rewrite`, `summary`) to native requests. The compiler chooses only the fixed project schema, and auxiliary output is parsed separately from public answers. Rewrite output is≤256 tokens and summary output≤384. Raw auxiliary responses stay in the private usage ledger, never graph checkpoints or public answer events. Legacy answer fingerprints omit the default field, preserving frozen requests. Explicit experiments use the same egress/accounting rules; auxiliary readiness and semantic promotion have not been established by live account evaluation.
