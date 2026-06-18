# ADR-002: PostgreSQL owns metadata vectors and keyword index

**Superseded in specification v2 by ADR-012 and ADR-013. Historical v1 rationale below; not an implementation instruction.**


Status: proposed baseline, awaiting user architecture review. Date:2026-10-10.

## Context

Atomic document publication and tenant consistency matter at100k chunks.

## Decision

PostgreSQL17+pgvector0.8.7+FTS; exact vector baseline, optional validated HNSW, rank fusion.

## Alternatives

Qdrant/Weaviate/Milvus are credible at larger vector scale; OpenSearch offers BM25 but adds state sync.

## Consequences

One durable source of truth; ANN filtering must be benchmarked; FTS is not BM25.

## Revisit trigger

Consider dedicated vector/search service only after working-set/p95 evidence and deletion/index reconciliation design.

## Evidence and detailed contract

[Design and research](../03-ai-engineering/retrieval.md); primary source citations are in research findings and references.
