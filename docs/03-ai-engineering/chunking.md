# Chunking specification

Default `structure_v1`: parse heading/paragraph/table/code structure, tokenize with pinned BGE tokenizer, greedily pack adjacent blocks under a heading to target 320 tokens and hard cap **384 tokens including heading prefix and special tokens**. Overlap up to 48 body tokens only between adjacent chunks within the same section, never across document/page provenance boundaries that cannot be represented. Heading prefix at most 32 tokens; full heading retained in metadata. Minimum 64 tokens is a packing preference, not a reason to drop short evidence. Embed exact `embedding_text`; cite original normalized `text` and offsets, never synthetic heading repetition as source prose.

Why 384 rather than 800: BGE's [documented 512-token limit](https://huggingface.co/BAAI/bge-small-en-v1.5) and cross-encoder pair budget. Count with actual tokenizer, no words/characters heuristic. If one block exceeds cap, sentence split then token split preserving character span. Code fenced blocks split at line boundaries and carry language/heading. Table rows repeat headers in embedding input but citation points to original row. Parent IDs identify a section; parent text is reconstructed from normalized blocks, not duplicated in vectors.

| Strategy | Benefit | Risk | Experiment |
|---|---|---|---|
| Fixed token windows | Reproducible simple baseline | Cuts semantic units | 256/32 and 384/48 |
| Recursive separators | Cheap paragraph preservation | PDF extraction noise changes boundaries | Compare against structure_v1 |
| Structure-aware default | Preserves sections/provenance | Parser-dependent | 320 target, 384 hard maximum |
| Semantic breakpoints | Possible coherence gain | Extra embedding compute, unstable thresholds | Stretch only with retrieval lift |
| Parent-child | Small retrieval with broader synthesis | Context bloat, citation mapping | Expand one adjacent sibling under same revision |
| Context enrichment | Disambiguates isolated passage | Synthetic context can introduce false facts/cost | Heading only core; LLM enrichment disabled |

Chunk UUID = UUIDv5(project namespace, workspace_id + revision_id + chunk_profile_hash + ordinal + text_sha256). Record content hash separately from identity. Metadata: document/revision/KB/workspace, ordinal, parent_ordinal, normalized char range, block range, page range nullable, heading_path, language, token_count, parser/chunker versions, original object digest, embedding profile. Contiguous offsets refer to the normalized block stream; `source_locator` retains original parser coordinates.

Property tests: no text loss except declared normalization; each body span covered; overlap bounded; same input/profile gives identical output; no chunk exceeds embedding cap; Unicode not cut inside code point; split boundaries not borrowed across tenants/docs; ordinal sequence complete. Retrieval experiment holds corpus/version/embedding/model fixed and changes only chunk configuration. Do not promote a setting based only on average chunk length.
