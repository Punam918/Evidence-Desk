# ADR-003: PostgreSQL durable jobs without baseline broker

Status: proposed baseline, awaiting user architecture review. Date:2026-10-10.

## Context

Accepted uploads/runs must survive worker failure without separate broker operations.

## Decision

Short SKIP LOCKED claims,30s fenced leases, heartbeat, idempotent stages and dead status.

## Alternatives

Celery/Redis reduce some scheduling code but add broker state and still need idempotency.

## Consequences

Custom lease/sweeper tests essential; NOTIFY is a hint not delivery guarantee.

## Revisit trigger

Revisit if queue coordination consumes>10%DB CPU or admission p95>200ms after tuning.

## Evidence and detailed contract

[Design and research](../04-backend-and-apis/backend-design.md); primary source citations are in research findings and references.
