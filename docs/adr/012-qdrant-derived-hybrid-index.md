# ADR-012 — Qdrant derived hybrid index

Status: selected v2 design, supersedes ADR-002 retrieval placement; 2026-10-10.

Choose PostgreSQL18 for authoritative content/permissions/jobs, Qdrant1.19 for dense+sparse search and private S3 for immutable originals/artifacts. This meets the user's requested stack and provides concrete portfolio work in retrieval evaluation and cross-store recovery. No measured superiority over pgvector is claimed.

Use one physical collection per immutable profile generation, shared across tenants with indexed mandatory filters; hydrate only authorized PostgreSQL chunks. FastEmbed BM25 sparse vectors and BGE384 dense vectors feed application RRF60 then MiniLM. Global sparse IDF is a documented relevance coupling. PostgreSQL manifests and a transactional outbox provide resumable publication/purge; no distributed transaction. Qdrant writes `wait=true`; published PostgreSQL pointer requires verified complete manifest. Tombstones override search immediately; purge is eventually retried.

Cost: an additional private service, backups/rebuilds,16-GiB sizing hypothesis, explicit consistency tests. Alternative pgvector remains valid for a smaller stack. Revisit collection-per-tenant for statistical isolation, or managed replicated Qdrant for an HA requirement. [Protocol](../05-database-and-storage/qdrant-index.md).
