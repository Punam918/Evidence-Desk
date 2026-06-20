# Architecture decisions

All decisions are proposed baseline for architecture review, not records of deployed systems.

- [ADR-001: Modular monolith with separate worker processes](001-modular-monolith-with-separate-worker-processes.md)
- [ADR-002: PostgreSQL owns metadata vectors and keyword index](002-postgresql-owns-metadata-vectors-and-keyword-index.md)
- [ADR-003: PostgreSQL durable jobs without baseline broker](003-postgresql-durable-jobs-without-baseline-broker.md)
- [ADR-004: Workspace-shared sources and creator-private conversations](004-workspace-shared-sources-and-creator-private-conversations.md)
- [ADR-005: Immutable revisions with atomic publication](005-immutable-revisions-with-atomic-publication.md)
- [ADR-006: Native paid and local adapters](006-native-paid-and-local-adapters.md)
- [ADR-007: Deterministic workflow and approved internal brief](007-deterministic-workflow-and-approved-internal-brief.md)
- [ADR-008: Evidence-valid final answers with provisional streams](008-evidence-valid-final-answers-with-provisional-streams.md)
- [ADR-009: CPU-first embeddings and bounded open inference](009-cpu-first-embeddings-and-bounded-open-inference.md)
- [ADR-010: Frozen paired evaluation over impressive demos](010-frozen-paired-evaluation-over-impressive-demos.md)
- [ADR-011: Single-host capstone and separate scale reference](011-single-host-capstone-and-separate-scale-reference.md)

- [ADR-012: Qdrant derived hybrid index](012-qdrant-derived-hybrid-index.md) — selected; supersedes002 retrieval placement.
- [ADR-013: LangGraph bounded durable workflows](013-langgraph-bounded-durable-workflows.md) — selected; supersedes007 mechanism.

- [ADR-014: Memory experiments before serving promotion](014-memory-experiments-before-serving-promotion.md).
- [ADR-015: Evaluation provenance and import trust](015-evaluation-provenance-and-import-trust.md).

- [ADR-016: Private operational telemetry](016-private-operational-telemetry.md).

- [ADR-017: Bounded replay and checkpoint retention](017-bounded-replay-and-checkpoint-retention.md).
- [ADR-018: Conversation purge and surviving thread tombstones](018-conversation-purge-and-surviving-thread-tombstones.md).
- [ADR-019: Source-derived content redaction](019-source-derived-content-redaction.md).
- [ADR-020: Conversation expiry through the existing purge queue](020-conversation-expiry-through-purge-queue.md).
- [ADR-021: Source object namespace purge](021-source-object-namespace-purge.md).
- [ADR-022: Research-driven reliability controls](022-research-driven-reliability-controls.md).

- [ADR-023: Networkless Compose parser service](023-networkless-compose-parser-service.md).
- [ADR-024: Local Compose bootstrap and identity](024-local-compose-bootstrap-and-identity.md).
- [ADR-025: Persisted run context for private history](025-persisted-run-context-for-private-history.md).
