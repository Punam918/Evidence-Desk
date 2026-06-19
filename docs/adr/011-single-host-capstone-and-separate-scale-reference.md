# ADR-011: Single-host capstone and separate scale reference

Status: proposed baseline, awaiting user architecture review. Date:2026-10-10.

## Context

Architecture should be implementable and inexpensive before HA spending.

## Decision

Compose/VM/private objects/OIDC; preserve separate APIs/workers for future managed scale.

## Alternatives

Kubernetes and self-host Langfuse add operations; serverless long streams/parsers need separate tradeoff review.

## Consequences

Single point of failure; explicit RPO/RTO and measured recovery rather than an HA claim.

## Revisit trigger

Move to managed multi-zone services when real availability/load requirements justify costs.

## Evidence and detailed contract

[Design and research](../09-infrastructure/deployment.md); primary source citations are in research findings and references.

## v2 sizing update

The current demonstration hypothesis is8 shared vCPU/16 GiB, Qdrant and PostgreSQL on a private network, local CPU retrieval, hosted generation, no resident8B generator. Recomputed hosted scenario is about$107.70/month before evaluation and listed exclusions; budget target$130. The original smaller topology is superseded. Load and restore evidence remain required, not inferred from RAM allocation.
