# ADR-006: Native paid and local adapters

Status: proposed baseline, awaiting user architecture review. Date:2026-10-10.

## Context

Support paid/open models as a product capability, not a screenshot-only checkbox.

## Decision

OpenAI Responses+Ollama mandatory; typed ModelRequest/Event and common conformance; Anthropic/Gemini later.

## Alternatives

Gateway/framework normalizes many APIs but may hide unsupported capability behavior.

## Consequences

Small adapter maintenance burden; provider limitations exposed and tested.

## Revisit trigger

Adopt gateway when provider count/operations justify it and conformance semantics remain unchanged.

## Evidence and detailed contract

[Design and research](../03-ai-engineering/model-abstraction.md); primary source citations are in research findings and references.
