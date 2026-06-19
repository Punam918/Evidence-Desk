# ADR-005: Immutable revisions with atomic publication

Status: proposed baseline, awaiting user architecture review. Date:2026-10-10.

## Context

Reprocessing must not expose partial retrieval indexes.

## Decision

Build/stage/verify new revision then swap active pointer transactionally; tombstones override snapshots.

## Alternatives

In-place updates simpler initially but break consistency and citation provenance.

## Consequences

Old citation-readable revisions consume storage; purge and generation migration are explicit.

## Revisit trigger

Revisit storage retention after real usage; do not drop immutability to save minor space.

## Evidence and detailed contract

[Design and research](../03-ai-engineering/document-ingestion.md); primary source citations are in research findings and references.

## v2 clarification

Qdrant index publication follows [ADR-012](012-qdrant-derived-hybrid-index.md): delivery outbox verifies points before ingestion finalizer swaps the PostgreSQL pointer. Atomic refers to that pointer transaction only. No shared DB/vector-store transaction is claimed; tombstones override both generations and reconciliation removes stale external writes.
