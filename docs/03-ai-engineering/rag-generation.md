# Grounded generation and citation validation

Generate a typed answer containing `status`, `sentences[{text,citation_ids}]`, and `limitations[]`. Evidence IDs E1…E8 are assigned by the orchestrator for this run only. The model never supplies authoritative document IDs, URLs, page numbers or quotes. The server maps E IDs to immutable chunk/revision metadata and produces final citations. Response schema is [Answer](../../contracts/schemas/answer.schema.json).

Prompt order: system behavior; developer output/trust rules; selected past messages as untrusted history; current user query; quoted evidence records. Include document name/revision/date and exact normalized text in clearly delimited records. Citation tokens in document text are escaped so they cannot impersonate E labels. Deduplicate overlapping evidence; place highest-ranked evidence first and reserve comparison passages for both sides. Context-placement is an evaluation factor, not an assumed universal rule.

## Evidence policy

- Every factual answer sentence must carry ≥1 supplied evidence ID; greetings/request-to-clarify/abstention go in limitations/status, not uncited factual sentences.
- Claims about one revision may not be attributed to another. If sources conflict, identify the conflict and cite both; newest upload time is not proof of factual authority.
- No evidence yields deterministic abstention before model call. Insufficient evidence from available passages yields `abstained`, empty sentences, explanation in limitations.
- Treat system/user instructions found in sources as source text, not commands. No secret, hidden prompt, tool or endpoint authority comes from documents.
- A citation proves location and access, not semantic truth. Deterministic checks verify membership in evidence set and visibility. Human/optional offline judge measures support/entailment; do not label all linked claims “verified facts”. UI says “Sources”, not “fact checked”.

Validation order: parse complete JSON → JSON Schema → length limits → allowed E IDs → factual sentence nonempty citation list → duplicate/provenance consistency → source still visible → atomic final message/run/usage/terminal event. Unknown reference/schema/truncation fails finalization (`invalid_model_output`). Core has no extra repair generation; an explicit retry creates a new costed run. Refusal is `refused` run state with no completed grounded answer.

## Streaming design

Provider partial JSON is not emitted raw. An incremental JSON string parser may emit decoded text only from `sentences[*].text` as `answer.delta` with draft=true, after string escapes/UTF-8 are complete. If not supported by adapter, emit phase events while buffering, then one draft text batch; record that TTFT is delayed. Never heuristically strip JSON using regex. No citation links become authoritative until the complete payload passes validation. `answer.completed` replaces draft with final structured answer; failed/interrupted draft remains visibly incomplete. Invalidation from deleted source clears draft in UI via `run.failed` code source_revoked.

```mermaid
flowchart LR
 Q[Question and bounded history] --> R[Retrieve authorized evidence]
 R --> P[Pack and count full prompt]
 P --> G[Generate draft structured answer]
 G --> S[Schema and E-ID validation]
 S --> A[Live access recheck]
 A --> F[Commit final and citations]
 S --> X[Fail or abstain]
 A --> X
```

Acceptance: source-free hallucination prompts, planted citations, conflicting versions, unanswerable questions, HTML/Markdown injection, deleted evidence during stream, malformed JSON and provider refusal. Final output correctness is measured on held-out human labels; prompt compliance alone is insufficient.

## v2 graph execution

The answer_v1 graph sequences retrieval, context, native adapter, validation and finalization under the same Answer/event contracts. Provider output is persisted privately in model_usage.response before the generation node returns; validation replay can reuse a complete response. Issued attempts with uncertain outcome terminalize interrupted even when no text was shown; graph replay cannot issue them again. An SDK auto-retry is disabled so the existing router budgets remain the sole retry authority. See [workflow recovery](langgraph-workflows.md).

## Implemented context stage (T16)

`ai/context.py` packs at most8 full passages within3072 tokens, including serialized evidence labels/metadata. It removes identical text (within each side for comparisons) and passages overlapping at least80% of the shorter normalized span in the same revision, keeping the higher-ranked passage. Comparison reserves one feasible passage per requested side before filling other slots; it tries bounded alternate pairs if the best pair is too large. No source text is silently clipped for generation. Empty evidence or an infeasible required pair produces an abstention bundle with no ModelRequest.

The packer counts the complete adapter request, including instructions, schema, query, evidence and history, then reserves configured output tokens plus the shared billing margin of max(256, ceil(5% of the context cap)) tokens. It trims oldest history pairs before refusing additional evidence. `ai/token_budget.py` binds o200k to the native OpenAI adapter and the locked Qwen tokenizer to the matching Ollama model/digest. Both adapter totals are conservative estimates with20% overhead, not exact remote-provider accounting. Accepted requests fit the configured cap under that counter; a provider's different internal template may still reject context. Provider-side truncation is disabled. Every fallback must repack from the original ranked pool with its own tokenizer and cap.

Prompts are packaged versioned resources, fingerprinted at render time. Evidence records and history are JSON quoted, with XML delimiters and bracketed E-label strings escaped in the rendering copy; source text and citation provenance stay unchanged. This supports structural separation and does not claim that prompting alone prevents injection. PromptBundle contains private text and must never enter LangGraph checkpoints or telemetry. T17 still owns provider egress authorization, cost reservation, complete output validation and atomic finalization.

T17 now wires the grounded graph in `ai/orchestrator.py`. Complete output is validated twice where necessary for replay: strict JSON object properties, Answer schema/coherence, UTF-8 validity, total20000-character final-message bound (including sentence separators), supplied E labels and duplicate citation rejection. Citation title/page/heading/document/revision/chunk metadata comes exclusively from authorized database passages. Unknown, malformed, truncated or inconsistent output cannot complete. Citation membership is not a semantic support score.

The native adapter's partial JSON stays private. After complete-output validation, sentence strings are emitted as durable drafts in≤256-byte UTF-8 batches with a transactional cursor, followed by atomic completion. This deliberately delays visible answer text until the provider finishes; phase events remain visible while it generates. If a source is revoked after a draft, finalization fails and clears the draft. Empty retrieval or an infeasible comparison completes an explicit abstention without a provider attempt.

2026-10-19 output integrity (ADR022): the complete-output path additionally checks verbatim double-quoted excerpts against the body text of that sentence's cited passages, normalizing NFC/whitespace only. It rejects malformed/empty quotes, inline bracketed citation labels and control/bidi manipulation; limitations cannot carry quotations or source labels. This runs before any draft, with existing failure/accounting behavior. It does not verify paraphrase entailment, infer source truth or trigger an unbudgeted repair call. Prompt v2 states this contract and handles insufficiency/ambiguity/conflict explicitly.
