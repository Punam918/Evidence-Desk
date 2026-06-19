# ADR-004: Workspace-shared sources and creator-private conversations

Status: proposed baseline, awaiting user architecture review. Date:2026-10-10.

## Context

Fine-grained inherited document ACL connectors are infeasible core scope.

## Decision

All current members read workspace docs; only creator reads conversation/run/brief; admin gets aggregates.

## Alternatives

Per-document ACLs add synchronization/metadata complexity; all-admin chat visibility harms privacy.

## Consequences

Simpler explicit role contract; not suitable for organization requiring per-document secrecy.

## Revisit trigger

Add per-document grants only with comprehensive retrieval/cache/citation/replay tests and migration.

## Evidence and detailed contract

[Design and research](../01-product/prd.md); primary source citations are in research findings and references.
