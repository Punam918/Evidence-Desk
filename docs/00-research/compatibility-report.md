# Implementation compatibility — 2026-10-11

T01 passed on this Linux x86_64 workstation with Python 3.14.8, Node 24.21.0 and pnpm 10.28.2. This records executable compatibility; it is not a retrieval-quality, latency or production-security certification.

`uv.lock` resolves 188 packages across optional/platform branches. The installed selection includes CPU PyTorch 2.14.1+cpu, sentence-transformers 6.1.0, FastEmbed 0.9.0, FastAPI 0.143.0, SQLAlchemy 2.1.4, psycopg 3.3.6, LangGraph 1.2.14 and PostgreSQL saver 3.1.2. `pnpm-lock.yaml` freezes the frontend and generator graph. PostgreSQL 18.6 and Qdrant 1.19.2 images are digest-pinned in `infra/images.lock`.

Executed checks:

- Dependency installation from locks; React 19.3 / TypeScript 7.0.2 / Vite 8.3.4 production shell build and generated OpenAPI types.
- Real CPU BGE embeddings: two finite, normalized 384-dimensional outputs. MiniLM ranks a relevant fixture ahead of an unrelated fixture. FastEmbed BM25 produces nonempty finite document/query sparse vectors. Immutable revisions and every downloaded artifact SHA-256 are in `contracts/config/model-artifacts.lock.json`.
- Real PostgreSQL and Qdrant probes: filtered dense+sparse search, read-only key denial, deterministic upsert/delete, dedicated saver schema, interrupt persistence across a fresh connection/graph, resume, completed replay and checkpoint deletion. See `docs/10-testing/runtime-contract-results.json`.
- PDF read/write, DOCX text roundtrip and HTML parsing. See `docs/10-testing/build-compatibility-results.json`.
- `pip-audit` for the installed Python selection and `pnpm audit` reported zero known advisories on this date. This is a point-in-time result, not a guarantee against undisclosed vulnerabilities. The report includes installed dependency license declarations; the two dense/rerank artifacts declare MIT and Apache-2.0 respectively. Local generation artifacts receive a separate license/digest gate in T08.

Compatibility decisions: `openapi-typescript` 7.13 requires TypeScript 5, so the root generator uses 5.9.3 while the application retains TypeScript 7. CPU PyTorch has an explicit package index; the default CUDA distribution was excluded from the deployment lock. No custom Hugging Face code is executed. Hosted model availability and Qwen generation throughput are deliberately unclaimed until their later conformance gates.

Reproduce with `uv sync --all-packages --all-extras --locked`, `pnpm install --frozen-lockfile`, `pnpm api:generate`, `pnpm build`, and `uv run python scripts/validate_runtime_contracts.py`. The artifact resolver is an operator update tool; normal inference must use the committed revisions and hashes rather than resolving mutable heads.
