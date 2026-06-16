# Implementation repository and coding standards

Implementation is in progress. The map below is the target structure; modules are added when their backlog task is implemented, not as empty placeholders. Current completion evidence is in `tasks/status.json`.

```text
apps/
  api/
    pyproject.toml
    src/evidencedesk/
      app.py                  # FastAPI composition root
      config.py               # typed environment/policy/model config
      routes/                 # HTTP validation and transport mapping
      identity/ tenancy/      # sessions and role/object authority
      knowledge/ chat/        # business services
      ingestion/              # upload, isolated parsers, normalize, chunk, publish
      embeddings/ retrieval/  # inference adapters, Qdrant dense/sparse/fusion/rerank
      ai/                     # context, router, orchestrator, answer validation, memory
      providers/              # native provider transports only
      tools/ workflows/       # registry, authorization, approvals, bounded compare
      billing/                # reservations, usage, aggregate costs
      jobs/ workers/          # leases, stage entrypoints, recovery
      lifecycle/              # tombstones, purge, reconciliation
      observability/          # redacted logs/spans/metrics
      prompts/                # versioned templates and schemas
      db/                     # models/repositories/privileged SQL routines
    migrations/               # Alembic authoritative executable migrations
  web/
    src/app/                  # router/session/workspace boundary
    src/features/             # chat, knowledge, evidence, compare, reports, settings
    src/components/           # accessible shared primitives
    src/lib/                  # safe renderer and typed SSE client
packages/api-client/          # generated OpenAPI types/client, not handwritten duplicates
contracts/                    # HTTP, events, tool, answer, config schemas
evals/                        # source manifests, labels, runner, configs, reports
infra/compose/ infra/tofu/     # local and cloud deployment declarations
infra/monitoring/              # dashboards/alerts/collector policy
tests/unit/ tests/property/ tests/integration/ tests/contracts/
tests/security/ tests/ui/ tests/chaos/ tests/load/
scripts/                      # contract validation, dev, backup, restore, eval entrypoints
docs/ tasks/                  # architecture, rationale, task evidence
```

## Important boundary contracts

| Module | Public interface | Allowed dependencies | Config / test obligation |
|---|---|---|---|
| routes | OpenAPI operation handlers | domain services, public schemas | HTTP errors/auth/CSRF/idempotency examples |
| identity/tenancy | AuthContext and authorization methods | identity DB functions/repositories | OIDC settings, negative role/scope tests |
| ingestion | DocumentService/Parser/Chunker | objects, jobs, embeddings, repositories | parser/profile limits, golden/kill tests |
| retrieval | Retriever/RetrievalResult | scoped DB, Qdrant read client, embedding/rerank adapters | profile generation, exact-vs-ANN and scope tests |
| ai | Orchestrator/ContextBuilder/Router | retrieval, providers, billing, tools | prompt/model registry, grounding/token tests |
| providers | ModelAdapter only | HTTP SDK/client, typed ModelRequest | endpoint/secret refs; no DB/auth access; conformance |
| tools | ToolExecutor | authorized domain repositories and LangGraph | registry/schema/approval expiry; no arbitrary HTTP |
| billing | reserve/settle/report | DB transactions and price registry | micro-USD, concurrent race and unknown-usage tests |
| jobs | claim/checkpoint/finish | DB routines and task dispatch | lease/backoff/admission; crash/fencing tests |
| frontend | generated client/SSE | UI components/query cache | scope keys/a11y/XSS/reconnect tests |
| evals | versioned experiment runner | same domain interfaces | immutable datasets/model/config; metric math tests |

Python strict typing at module boundaries, no untyped dictionaries as domain objects, Pydantic at trust boundaries; dataclasses/Protocols for internal stable interfaces. Ruff+format, mypy strict on domain code; TypeScript strict+ESLint+formatter; pytest, Hypothesis for invariants, Playwright+axe for UI. Avoid a generic abstraction until at least two genuine implementations exist; ModelAdapter is justified by two mandatory providers.

Dependency management uv/pnpm lockfiles, declared Python/Node engines, digested images/model artifacts. Upgrades grouped into small PRs with license/vulnerability and conformance checks. Never copy vendor SDK types into public schema or leak raw exception class names. Expected failures are typed domain errors mapped at transport boundary; unexpected errors get request ID and redacted logs.

Commits use clear scopes (`feat(retrieval): ...`, `fix(auth): ...`, `docs(api): ...`), one reviewable behavior per PR. PR description states trigger/problem, resulting behavior, validation and limitations; link requirement/task IDs. Contract changes regenerate client and examples, migrate DB as needed, update ADR and tests. No blanket exception catch returning success, disabled tenant tests, TODO security or fake benchmark data. Validate meaningful risk-bearing changes; avoid tests that merely assert copied constants.

## Additional v2 modules and library usage

Plan `retrieval/qdrant_index.py` as the only SearchIndex client; `ingestion/index_outbox.py` for immutable delivery; `workflows/answer.py`, `workflows/brief.py`, `workflows/state.py`, `workflows/checkpoint_codec.py`, `workflows/thread_gate.py` for graph contracts; `lifecycle/index_reconcile.py` and `lifecycle/checkpoint_purge.py` for recovery. Graph nodes call domain services through typed interfaces; no permission logic hidden in prompts or provider wrappers. Workflows depend on LangGraph, not vice versa.

React Router handles authenticated SPA routes; TanStack Query keys include workspace/user identity and invalidation clears on logout/switch. Use generated openapi-typescript types, native authenticated fetch streaming, Vitest component/reducer cases and Playwright journeys; don't add a second backend or framework-specific AI stream protocol. Current library versions and resolution gates are in current-stack.md.
