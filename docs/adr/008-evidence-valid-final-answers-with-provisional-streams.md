# ADR-008: Evidence-valid final answers with provisional streams

Status: proposed baseline, awaiting user architecture review. Date:2026-10-10.

## Context

Fast streaming cannot make incomplete citation data trustworthy.

## Decision

Persist decoded draft text; final schema/E-ID/live-source validation replaces draft; no model splice after failure.

## Alternatives

Buffer entire response sacrifices TTFT; raw token display risks JSON/injection confusion.

## Consequences

Incremental parser/replay adds code; semantic claim support still evaluated offline.

## Revisit trigger

Buffer for adapters unable to decode structured text safely; report that latency separately.

## Evidence and detailed contract

[Design and research](../03-ai-engineering/rag-generation.md); primary source citations are in research findings and references.
