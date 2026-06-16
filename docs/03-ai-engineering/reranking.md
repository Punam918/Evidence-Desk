# Reranking contract

Default candidate `cross-encoder/ms-marco-MiniLM-L6-v2` ([publisher](https://huggingface.co/cross-encoder/ms-marco-MiniLM-L6-v2)). The reranker receives only already-authorized candidate text. It never fetches documents. Pin model/tokenizer revisions; float32 CPU first, ONNX only after numeric rank parity test. CPU task runs in bounded inference process separate from the API event loop.

Input: original query and up to 20 RRF-ordered passages. Pair token cap 512 including special tokens; query max 96 reranker tokens for pair scoring, document remainder. If query longer, keep deterministic first 48/last 48 reranker tokens and record `rerank_query_clipped`; original complete retrieval and generation query remain unchanged. Passage chunk target was chosen small enough to minimize clipping, but tokenizer mismatch still requires explicit checks. Truncate only reranking copy; preserve exact full source for generation/citation.

Batch size 8, timeout 800 ms initial target and 2s hard bound, one inference slot. Queue full/time cap/NaN/wrong output count falls back to RRF with `rerank_unavailable`, no additional retry in serving path. Stable sort descending score then RRF rank then UUID. Keep scores internal; do not show sigmoid output as “confidence”. Cross-encoder trained on MS MARCO may not generalize to runbooks or abbreviations; evaluation decides whether it earns its latency.

Compare V1 vs V2 on identical candidate pools, measure nDCG@10 and Recall@8 plus p95 latency/CPU. Error taxonomy: relevant passage absent from candidate pool (retrieval issue), relevant passage demoted (reranker issue), truncated pair loses matching term, duplicates consume slots, irrelevant high score. A larger BGE reranker, hosted reranker or LLM judge reranker is stretch: must improve paired results within cost/latency budget and satisfy privacy policy. Never use a different corpus to claim reranking improvement.

Tests: stable ties, duplicated chunk, multilingual unsupported input, max-length pair, invalid floats, batch-order preservation, unauthorized candidate rejection, timeout fallback recorded in final run metadata.

## Current challenger

Evaluate Qwen3-Reranker-0.6B as specified in [current stack](../00-research/current-stack.md), using its publisher scoring/instruction protocol and separate resource profile. Never treat a generative yes/no reranker as the MiniLM logit interface without a conformant adapter. Promote only after frozen paired retrieval and latency/RAM gates; default tiny CPU reranker remains a reproducible reference.

## Implemented T16 boundary

`retrieval/rerank.py` rechecks live membership and reloads every candidate from PostgreSQL before scoring. `rerank_process.py` starts the fixed module `rerank_entry.py` in an isolated Python child with an allowlisted environment and offline model loading. Startup/warmup is a lifecycle operation with a45s bound; it is never performed as a serving retry. Serving has no waiting queue: the PostgreSQL advisory slot3 and local single-flight guard shed immediately. Slot1 remains generation and slot2 embedding/query encoding.

The child loads verified local safetensors, uses four CPU threads and batches8. It constructs paired BERT IDs directly, keeping first48/last48 query IDs when needed, and returns finite raw logits plus original token counts/clipping flags. The parent verifies identity order, count and model/library fingerprint. A2s scoring deadline kills and reaps the child before releasing its slot; cancellation also reaps it. Timeout, invalid protocol or dead process degrades to the original RRF order, with no retry of that request. A dead child stays unavailable until the worker lifecycle explicitly starts another; T17 composes that lifecycle with the chat worker.

Reranking retains all20 ordered candidates for the context packer, which selects at most8 while reserving both comparison sides. This avoids dropping the only RIGHT passage before packing. Raw logits remain internal, and neither clipping nor ordering rewrites stored source text. Unicode input is encoded without translation; multilingual ranking quality is outside the English baseline's validated scope.

See [T16 verification](../10-testing/rerank-context-report.md) for synthetic evidence and limitations. The800ms target is not a measured p95 guarantee.
