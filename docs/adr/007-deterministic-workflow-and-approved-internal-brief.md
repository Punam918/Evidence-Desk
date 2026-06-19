# ADR-007: Deterministic workflow and approved internal brief

**Superseded in specification v2 by ADR-012 and ADR-013. Historical v1 rationale below; not an implementation instruction.**


Status: proposed baseline, awaiting user architecture review. Date:2026-10-10.

## Context

Tool safety and recovery must be demonstrable under modest scope.

## Decision

Two-revision comparison and hash-bound approval, maximum3 calls; no arbitrary network/code.

## Alternatives

Open-ended agents add loops/spend/effects; LangGraph useful for more complex durable workflows.

## Consequences

Less autonomous but predictable; fixed controller success must not be marketed as general reasoning.

## Revisit trigger

Adopt graph engine only with real branching/checkpoint requirements and tests.

## Evidence and detailed contract

[Design and research](../03-ai-engineering/agents-and-tools.md); primary source citations are in research findings and references.
