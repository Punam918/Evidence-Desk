# ADR-010: Frozen paired evaluation over impressive demos

Status: proposed baseline, awaiting user architecture review. Date:2026-10-10.

## Context

A handful of selected answers cannot establish quality.

## Decision

400 reviewed questions with100/300 split, four retrieval variants, paired stats, human support labels and published failures.

## Alternatives

LLM-only judge or smoke set cheaper but unreliable as portfolio proof.

## Consequences

Labeling is substantial work; optional second-review limitation must be disclosed.

## Revisit trigger

Increase sample size when uncertainty prevents a defensible decision; do not tune on held-out set.

## Evidence and detailed contract

[Design and research](../07-evaluation/evaluation-plan.md); primary source citations are in research findings and references.
