# ADR-009: CPU-first embeddings and bounded open inference

Status: proposed baseline, awaiting user architecture review. Date:2026-10-10.

## Context

Portfolio must be affordable without continuous GPU rental.

## Decision

Local BGE384+MiniLM CPU; hosted generation public demo; optional Qwen Q4 workstation or temporary GPU.

## Alternatives

Paid embeddings simplify serving but add privacy/cost; larger open models increase hardware burden.

## Consequences

English-first; local CPU latency distinct; no equal-quality assumption.

## Revisit trigger

Multilingual/bigger models require labeled recall/grounding evidence and resource budget.

## Evidence and detailed contract

[Design and research](../09-infrastructure/cost-model.md); primary source citations are in research findings and references.
