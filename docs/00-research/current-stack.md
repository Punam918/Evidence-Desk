# Current stack and upgrade policy — 2026-10-10

Specification v2 adopts Qdrant and LangGraph as core components. This is a selected implementation target, not a runtime compatibility certificate. Latest stable package metadata was fetched directly from publisher registries; the [machine inventory](releases-2026-10-10.json) records versions, publication dates and source URLs. T01 must resolve a compatible complete dependency graph, install it on target platforms and record immutable hashes. Do not treat this inventory as `uv.lock` or `pnpm-lock.yaml`.

## Selected runtime and libraries

| Layer | Selected target | Why / release gate |
|---|---|---|
| Python | CPython 3.14.8, standard GIL build | Current bugfix line; Python 3.15.0 was released October 9. Avoid day-one native-wheel risk. Verify torch/ONNX/tokenizers/parser wheels in T01; document any necessary downgrade, never silently substitute. [Python releases](https://www.python.org/downloads/) |
| Web runtime | Node24 LTS, current security patch at lock time | Build/runtime tooling compatibility; Node26 is Current on research date. [Node schedule](https://nodejs.org/en/about/previous-releases) |
| Database | PostgreSQL18.6 | Supported stable major/minor in official table; PostgreSQL19 is still beta in the source observed. [Support policy](https://www.postgresql.org/support/versioning/) |
| Vector retrieval | Qdrant1.19.2 + qdrant-client1.19.1 | Named dense/sparse vectors, payload indexes, bounded Query API requests; [release](https://github.com/qdrant/qdrant/releases/tag/v1.19.2) |
| Workflow | LangGraph1.2.14 + langgraph-checkpoint-postgres3.1.2 | StateGraph for bounded ask/compare; separate interrupt/resume brief workflow; no hosted Agent Server required |
| API/domain | FastAPI0.143, Pydantic2.14, SQLAlchemy2.1, Alembic1.20, psycopg3.3 | Typed async I/O, explicit authorization and transactions; library patches below are candidates for conformance |
| Frontend | React19.3, TypeScript7, Vite8, React Router8, TanStack Query5 | Authenticated SPA; verify TypeScript/plugin compatibility as a unit, generated OpenAPI types, fetch-based SSE |
| Inference | sentence-transformers6.1, FastEmbed0.9 for sparse BM25 | CPU dense/rerank process and local sparse encoder; immutable artifacts, no remote code; no mandatory hosted embeddings |
| Provider transport | Native OpenAI SDK3.28 + HTTPX/Ollama; secondary native SDKs in T22 | Responses API for paid route; do not wrap every call in overlapping agent frameworks |
| Tests/operations | pytest9, Hypothesis, Vitest5, Playwright1.64, OTel1.45, Prometheus, Docker Compose | Meaningful permission/recovery/evaluation gates; exact supplementary tooling pins resolved in T01 |

These are design judgments for a solo capstone. Qdrant and LangGraph add meaningful search and recovery work, while PostgreSQL jobs continue to own admission/leases. Redis, Kafka, Kubernetes, autonomous multi-agent teams, a second RAG framework and a self-hosted LLM observability platform are not required.

## Observed package metadata

| Package | Latest stable observed | Published UTC | Primary registry |
|---|---|---|---|
| pypi:fastapi | 0.143.0 | 2026-10-08T12:29:44.853354Z | [metadata](https://pypi.org/pypi/fastapi/json) |
| pypi:pydantic | 2.14.0 | 2026-10-08T14:34:46.864517Z | [metadata](https://pypi.org/pypi/pydantic/json) |
| pypi:sqlalchemy | 2.1.4 | 2026-10-07T18:17:21.503212Z | [metadata](https://pypi.org/pypi/sqlalchemy/json) |
| pypi:alembic | 1.20.0 | 2026-09-11T19:09:12.880981Z | [metadata](https://pypi.org/pypi/alembic/json) |
| pypi:uv | 0.13.0 | 2026-10-09T19:45:18.048001Z | [metadata](https://pypi.org/pypi/uv/json) |
| pypi:langgraph | 1.2.14 | 2026-10-06T14:41:18.129073Z | [metadata](https://pypi.org/pypi/langgraph/json) |
| pypi:langgraph-checkpoint-postgres | 3.1.2 | 2026-08-07T20:39:58.994834Z | [metadata](https://pypi.org/pypi/langgraph-checkpoint-postgres/json) |
| pypi:qdrant-client | 1.19.1 | 2026-09-16T06:43:12.428811Z | [metadata](https://pypi.org/pypi/qdrant-client/json) |
| pypi:fastembed | 0.9.0 | 2026-10-07T16:38:49.150256Z | [metadata](https://pypi.org/pypi/fastembed/json) |
| pypi:psycopg | 3.3.6 | 2026-09-18T13:15:29.374570Z | [metadata](https://pypi.org/pypi/psycopg/json) |
| pypi:sentence-transformers | 6.1.0 | 2026-09-18T10:44:22.841108Z | [metadata](https://pypi.org/pypi/sentence-transformers/json) |
| pypi:openai | 3.28.0 | 2026-10-09T20:50:12.282405Z | [metadata](https://pypi.org/pypi/openai/json) |
| pypi:httpx | 0.28.1 | 2024-12-06T15:37:21.509172Z | [metadata](https://pypi.org/pypi/httpx/json) |
| pypi:pytest | 9.1.1 | 2026-06-19T10:58:31.347074Z | [metadata](https://pypi.org/pypi/pytest/json) |
| pypi:ruff | 0.17.0 | 2026-10-09T19:46:38.695158Z | [metadata](https://pypi.org/pypi/ruff/json) |
| pypi:opentelemetry-sdk | 1.45.1 | 2026-10-06T17:32:55.040419Z | [metadata](https://pypi.org/pypi/opentelemetry-sdk/json) |
| pypi:pypdf | 6.20.0 | 2026-10-09T10:49:36.882910Z | [metadata](https://pypi.org/pypi/pypdf/json) |
| pypi:authlib | 1.8.0 | 2026-08-30T12:12:33.162089Z | [metadata](https://pypi.org/pypi/authlib/json) |
| pypi:boto3 | 1.43.111 | 2026-10-09T19:27:47.599851Z | [metadata](https://pypi.org/pypi/boto3/json) |
| npm:react | 19.3.0 | 2026-09-09T17:21:30.071Z | [metadata](https://registry.npmjs.org/react) |
| npm:vite | 8.3.4 | 2026-10-08T12:07:04.441Z | [metadata](https://registry.npmjs.org/vite) |
| npm:typescript | 7.0.2 | 2026-07-08T15:55:18.431Z | [metadata](https://registry.npmjs.org/typescript) |
| npm:@tanstack/react-query | 5.104.1 | 2026-10-02T10:04:57.020Z | [metadata](https://registry.npmjs.org/@tanstack/react-query) |
| npm:react-router | 8.4.0 | 2026-09-15T15:23:42.214Z | [metadata](https://registry.npmjs.org/react-router) |
| npm:vitest | 5.0.3 | 2026-09-30T11:30:42.429Z | [metadata](https://registry.npmjs.org/vitest) |
| npm:@playwright/test | 1.64.0 | 2026-10-07T20:44:27.702Z | [metadata](https://registry.npmjs.org/@playwright/test) |
| npm:openapi-typescript | 7.13.0 | 2026-02-11T16:02:25.378Z | [metadata](https://registry.npmjs.org/openapi-typescript) |
| github:qdrant/qdrant | v1.19.2 | 2026-10-05T14:16:50Z | [metadata](https://api.github.com/repos/qdrant/qdrant/releases/latest) |

## Model freshness and reproducibility

Choose `gpt-6-luna` as the paid default candidate, with `gpt-6.1-sol` as the disabled-until-evaluated quality route. The [current OpenAI catalog](https://developers.openai.com/api/docs/models) positions these for cost-sensitive work and stronger reasoning respectively. Enable only after schema/stream/usage/account probes and held-out evaluation. Preserve GPT-4.1 mini as an optional dated reference, not the default. If a current alias lacks an immutable API snapshot, record requested and returned model IDs, provider fingerprint when exposed, exact date/config and re-evaluate alias drift; do not claim bitwise reproducibility.

Retain BGE-small384/MiniLM and Qwen3:8b as explicit inexpensive local reference candidates, not claims of latest or best weights. New models are evaluated against them using the same corpus, resources and privacy policy. A larger embedding or reranker is promoted only after paired recall/latency/RAM improvement; this avoids forcing a GPU merely to list newer weights. T01 records a fresh publisher/model-runtime shortlist and supported artifact digests; local reference may remain the measured winner. No quality result has been measured here.

## Dependency and supply-chain contract

Use uv workspaces, frozen lock installation (`uv sync --locked`) and pnpm frozen lockfiles, separate runtime/test groups, explicit engine ranges, container/model digests and an SBOM. [uv documentation](https://docs.astral.sh/uv/concepts/projects/sync/) distinguishes a current lock check from an unchecked frozen assumption. CI verifies lock freshness and installs without resolution. Do not check in guessed lockfiles. Dependabot/Renovate opens small grouped updates; a maintainer reviews release notes, advisories and license changes. Security patches are expedited through the same tests; major upgrades require migration and rollback evidence.

T01 gates: resolve/install cleanly on Linux amd64; run Postgres migrations and saver setup, Qdrant collection/filter/dense+sparse round-trip, real384-d embedding and parser fixture, graph restart/interrupt replay, native SDK scripted streaming and frontend production build. Record dependency tree/conflicts and artifact hashes. This documentation revision validates contracts separately; it does not mark T01 complete.

## Current open-weight challengers to test

The newer [Qwen3.5-4B](https://huggingface.co/Qwen/Qwen3.5-4B) is the first local-generation challenger to Qwen3:8b; resolve the exact supported Ollama/llama.cpp artifact rather than guessing a runtime tag. Test schema adherence, citations, non-thinking configuration, latency and quantized RAM on the same workload. [Qwen3-Embedding-0.6B](https://huggingface.co/Qwen/Qwen3-Embedding-0.6B) and [Qwen3-Reranker-0.6B](https://huggingface.co/Qwen/Qwen3-Reranker-0.6B) are current retrieval-family challengers with their own token/instruction/dimension contracts. They are larger than the tiny CPU references and cannot inherit BGE chunk/profile settings blindly. T01 inventories supported artifacts; T25 reports paired quality/resource results. No claim these are globally latest, best or already compatible is made.

## Secondary hosted routes

Current [Gemini catalog](https://ai.google.dev/gemini-api/docs/models) lists stable `gemini-3.8-flash` and recommends newer models for new projects rather than access-limited2.5 routes. It replaces the old disabled Google candidate. Its [promotional pricing](https://ai.google.dev/gemini-api/docs/pricing) expires2026-12-31; registry expiry blocks enabling stale cost rates. Paid tier only for workspace content. [Claude catalog](https://platform.claude.com/docs/en/models/overview) still supports selected `claude-sonnet-5-5`; higher-priced Fable/Opus are not needed for this bounded RAG default. Both secondary routes stay disabled until native adapter/schema/usage/privacy/account probes and paired evaluation.
