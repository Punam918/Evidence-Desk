# ADR-001: Modular monolith with separate worker processes

Status: proposed baseline, awaiting user architecture review. Date:2026-10-10.

## Context

One engineer needs clear Python AI boundaries and real asynchronous processing.

## Decision

FastAPI shared package with API/chat/ingest/maintenance entrypoints and typed service interfaces.

## Alternatives

Django/DRF emphasizes CRUD/admin; NestJS introduces Python service boundary; microservices multiply deployments.

## Consequences

Shared schema/code simplifies transactions; process isolation still required for CPU/parsers.

## Revisit trigger

Revisit if independent team ownership/deployment or measured bottleneck cannot be isolated as a worker.

## Evidence and detailed contract

[Design and research](../00-research/ecosystem-and-decisions.md); primary source citations are in research findings and references.
