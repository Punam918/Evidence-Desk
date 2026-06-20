# Documentation index

Read the [root README](../README.md) first. All artifacts describe the planned implementation unless explicitly marked as executed specification checks.

## 00-research

- [ecosystem-and-decisions.md](00-research/ecosystem-and-decisions.md)
- [project-selection.md](00-research/project-selection.md)
- [sources.json](00-research/sources.json)

## 01-product

- [nonfunctional-requirements.md](01-product/nonfunctional-requirements.md)
- [prd.md](01-product/prd.md)
- [requirements.json](01-product/requirements.json)

## 02-architecture

- [interfaces.md](02-architecture/interfaces.md)
- [master-architecture.md](02-architecture/master-architecture.md)
- [repository-and-standards.md](02-architecture/repository-and-standards.md)

## 03-ai-engineering

- [agents-and-tools.md](03-ai-engineering/agents-and-tools.md)
- [chunking.md](03-ai-engineering/chunking.md)
- [conversational-memory.md](03-ai-engineering/conversational-memory.md)
- [document-ingestion.md](03-ai-engineering/document-ingestion.md)
- [embeddings.md](03-ai-engineering/embeddings.md)
- [model-abstraction.md](03-ai-engineering/model-abstraction.md)
- [model-routing.md](03-ai-engineering/model-routing.md)
- [prompt-engineering.md](03-ai-engineering/prompt-engineering.md)
- [rag-generation.md](03-ai-engineering/rag-generation.md)
- [reranking.md](03-ai-engineering/reranking.md)
- [retrieval.md](03-ai-engineering/retrieval.md)

## 04-backend-and-apis

- [api-conventions.md](04-backend-and-apis/api-conventions.md)
- [backend-design.md](04-backend-and-apis/backend-design.md)
- [frontend-design.md](04-backend-and-apis/frontend-design.md)
- [streaming.md](04-backend-and-apis/streaming.md)

## 05-database-and-storage

- [schema.sql](05-database-and-storage/schema.sql)
- [storage-design.md](05-database-and-storage/storage-design.md)

## 06-security

- [reliability.md](06-security/reliability.md)
- [threat-model.md](06-security/threat-model.md)

## 07-evaluation

- [evaluation-plan.md](07-evaluation/evaluation-plan.md)

## 08-observability

- [telemetry.md](08-observability/telemetry.md)

## 09-infrastructure

- [cost-model.md](09-infrastructure/cost-model.md)
- [deployment.md](09-infrastructure/deployment.md)

## 10-testing

- [database-validation-results.json](10-testing/database-validation-results.json)
- [diagram-validation-results.json](10-testing/diagram-validation-results.json)
- [spec-validation-results.json](10-testing/spec-validation-results.json)
- [test-catalog.md](10-testing/test-catalog.md)

## adr

- [001-modular-monolith-with-separate-worker-processes.md](adr/001-modular-monolith-with-separate-worker-processes.md)
- [002-postgresql-owns-metadata-vectors-and-keyword-index.md](adr/002-postgresql-owns-metadata-vectors-and-keyword-index.md)
- [003-postgresql-durable-jobs-without-baseline-broker.md](adr/003-postgresql-durable-jobs-without-baseline-broker.md)
- [004-workspace-shared-sources-and-creator-private-conversations.md](adr/004-workspace-shared-sources-and-creator-private-conversations.md)
- [005-immutable-revisions-with-atomic-publication.md](adr/005-immutable-revisions-with-atomic-publication.md)
- [006-native-paid-and-local-adapters.md](adr/006-native-paid-and-local-adapters.md)
- [007-deterministic-workflow-and-approved-internal-brief.md](adr/007-deterministic-workflow-and-approved-internal-brief.md)
- [008-evidence-valid-final-answers-with-provisional-streams.md](adr/008-evidence-valid-final-answers-with-provisional-streams.md)
- [009-cpu-first-embeddings-and-bounded-open-inference.md](adr/009-cpu-first-embeddings-and-bounded-open-inference.md)
- [010-frozen-paired-evaluation-over-impressive-demos.md](adr/010-frozen-paired-evaluation-over-impressive-demos.md)
- [011-single-host-capstone-and-separate-scale-reference.md](adr/011-single-host-capstone-and-separate-scale-reference.md)
- [README.md](adr/README.md)

## Contracts and evaluation assets

- [OpenAPI](../contracts/openapi.yaml)
- [Evaluation fixtures](../evals/README.md)
- [Primary sources](references.md)

## v2 implementation contracts

- [Current stack and release policy](00-research/current-stack.md)
- [Observed package inventory](00-research/releases-2026-10-10.json)
- [Qdrant consistency and recovery](05-database-and-storage/qdrant-index.md)
- [LangGraph execution and approval](03-ai-engineering/langgraph-workflows.md)
- [Qdrant collection configuration](../contracts/config/qdrant.yaml)
- [Workflow graph contract](../contracts/config/workflows.yaml)
