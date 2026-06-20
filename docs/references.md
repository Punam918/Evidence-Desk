# Research references

Accessed 2026-10-10. Official documentation, publisher model cards, or original research. Findings are deliberately narrow; recommendations elsewhere are design judgments. No private account or benchmark was queried. Dynamic pages can change after this date.

- **R01** [OpenAI structured outputs](https://developers.openai.com/api/docs/guides/structured-outputs) — Provider schema support has limits; handle refusals and incomplete responses.
- **R02** [OpenAI streaming](https://developers.openai.com/api/docs/guides/streaming-responses) — Responses emits typed stream events.
- **R03** [OpenAI pricing](https://developers.openai.com/api/docs/pricing) — Standard short-context Luna input/output: $0.10/$0.50 per million tokens; Sol 6.1: $2/$10. Verify at deployment.
- **R04** [GPT-4.1 mini](https://developers.openai.com/api/docs/models/gpt-4.1-mini) — Dated snapshot 2025-04-14; listed input/output $0.40/$1.60 per million tokens.
- **R05** [GPT-6 Luna](https://developers.openai.com/api/docs/models/gpt-6-luna) — Current candidate; no account access or quality validated in this work.
- **R06** [Claude model catalog](https://platform.claude.com/docs/en/models/overview) — Catalog lists Sonnet 5.5 and $2/$10 input/output per million tokens; availability is account-specific.
- **R07** [Claude structured outputs](https://platform.claude.com/docs/en/build-with-claude/structured-outputs) — JSON output and tool schema controls require provider-specific adaptation.
- **R08** [Gemini structured output](https://ai.google.dev/gemini-api/docs/structured-output) — Supported JSON Schema subset requires application validation.
- **R09** [Gemini pricing](https://ai.google.dev/gemini-api/docs/pricing) — 2.5 Flash text input $0.30 and output $2.50 per million tokens, including thinking output.
- **R10** [pgvector](https://github.com/pgvector/pgvector) — 0.8.7 image tags listed; filtered approximate search can under-return; iterative scans available since 0.8.0.
- **R11** [PostgreSQL text search](https://www.postgresql.org/docs/current/textsearch-controls.html) — ts_rank and ts_rank_cd rank full-text matches; do not call these BM25.
- **R12** [PostgreSQL row security](https://www.postgresql.org/docs/current/ddl-rowsecurity.html) — Owners normally bypass policies; FORCE and runtime roles matter.
- **R13** [PostgreSQL queue locking](https://www.postgresql.org/docs/17/sql-select.html) — SKIP LOCKED supports queue-like consumers; not a general consistent-view mechanism.
- **R14** [PostgreSQL support](https://www.postgresql.org/support/versioning/) — Major support lifecycle informs supported baseline selection.
- **R15** [Qdrant multitenancy](https://qdrant.tech/documentation/manage-data/multitenancy/) — Payload tenant filters and partitioning options; application still owns authorization.
- **R16** [Weaviate multitenancy](https://docs.weaviate.io/weaviate/manage-collections/multi-tenancy) — Tenant-aware collection operations and tenant lifecycle.
- **R17** [Milvus multitenancy](https://milvus.io/docs/multi_tenancy.md) — Several isolation levels with different scale tradeoffs.
- **R18** [OpenSearch hybrid](https://docs.opensearch.org/latest/vector-search/ai-search/hybrid-search/index/) — Hybrid search combines lexical and neural results through search pipelines.
- **R19** [Elasticsearch similarity](https://www.elastic.co/docs/reference/elasticsearch/index-settings/similarity) — BM25 similarity is available; useful external lexical baseline.
- **R20** [FastAPI concurrency](https://fastapi.tiangolo.com/async/) — Async I/O does not turn CPU parsing/inference into nonblocking work.
- **R21** [FastAPI releases](https://fastapi.tiangolo.com/release-notes/) — Resolve and test dependency pins at foundation gate.
- **R22** [Django REST Framework](https://www.django-rest-framework.org/) — Mature API/auth/serialization ecosystem; candidate for CRUD-heavy product.
- **R23** [NestJS](https://docs.nestjs.com/) — TypeScript server framework; Python inference would introduce another language boundary.
- **R24** [LangGraph](https://docs.langchain.com/oss/python/langgraph/overview) — Durable stateful graph orchestration; justified by complex workflow recovery.
- **R25** [LlamaIndex](https://developers.llamaindex.ai/python/framework/) — Data/retrieval connectors and workflow ecosystem.
- **R26** [Haystack pipelines](https://docs.haystack.deepset.ai/docs/pipelines) — Explicit component pipelines with branching/loops.
- **R27** [vLLM serving](https://docs.vllm.ai/en/latest/serving/online_serving/openai_compatible_server/) — Compatible API is not universal parameter parity; chat templates matter.
- **R28** [Ollama compatibility](https://docs.ollama.com/api/openai-compatibility) — Useful local serving; supported endpoints/parameters must be tested.
- **R29** [llama.cpp](https://github.com/ggml-org/llama.cpp) — Quantized inference across CPU/GPU backends; operational alternative.
- **R30** [BGE small English](https://huggingface.co/BAAI/bge-small-en-v1.5) — 384 dimensions, 512-token sequence; query instruction and normalization are documented.
- **R31** [MiniLM reranker](https://huggingface.co/cross-encoder/ms-marco-MiniLM-L6-v2) — Cross-encoder candidate for bounded reranking; published benchmarks are not project results.
- **R32** [Qwen3 8B](https://huggingface.co/Qwen/Qwen3-8B) — 8.2B parameters; native 32768 context; non-thinking mode is supported.
- **R33** [Llama 3.1 8B](https://huggingface.co/meta-llama/Llama-3.1-8B-Instruct) — Alternative open-weight instruction model with its own license terms.
- **R34** [Mistral Small 3.1](https://huggingface.co/mistralai/Mistral-Small-3.1-24B-Instruct-2503) — 24B alternative has larger resource needs than 8B baseline.
- **R35** [BGE M3](https://huggingface.co/BAAI/bge-m3) — Multilingual and multiple retrieval representations; heavier candidate.
- **R36** [Ragas metrics](https://docs.ragas.io/en/stable/concepts/metrics/available_metrics/) — Retrieval/generation/tool metrics; judge-based scores need human calibration.
- **R37** [DeepEval](https://deepeval.com/docs/getting-started) — Test-oriented LLM evaluation alternative.
- **R38** [Promptfoo](https://www.promptfoo.dev/docs/intro/) — Prompt/provider test matrices and regression workflows.
- **R39** [Langfuse self-host](https://langfuse.com/self-hosting) — LLM tracing platform; full self-hosting adds infrastructure.
- **R40** [OpenTelemetry GenAI](https://opentelemetry.io/docs/specs/semconv/gen-ai/) — GenAI conventions evolve; pin instrumentation semantics.
- **R41** [OWASP prompt injection](https://cheatsheetseries.owasp.org/cheatsheets/LLM_Prompt_Injection_Prevention_Cheat_Sheet.html) — Layered controls; prompt text alone cannot enforce tool or data permissions.
- **R42** [OWASP uploads](https://cheatsheetseries.owasp.org/cheatsheets/File_Upload_Cheat_Sheet.html) — Validate file types and isolate processing/storage.
- **R43** [DigitalOcean pricing](https://www.digitalocean.com/pricing/droplets) — Regular Basic 8-vCPU/16-GiB monthly price $96 at retrieval.
- **R44** [Cloudflare R2 pricing](https://developers.cloudflare.com/r2/pricing/) — Standard storage $0.015/GB-month, A $4.50/million, B $0.36/million; free tier excluded from estimates.
- **R45** [Runpod pricing](https://www.runpod.io/pricing) — Listed RTX 4090 Pod $0.89/hour; inventory and region vary.
- **R46** [Celery tasks](https://docs.celeryq.dev/en/stable/userguide/tasks.html) — Acks/retries require idempotent task design; broker alone does not ensure exactly-once effects.
- **R47** [Docling](https://docling-project.github.io/docling/) — Document conversion and OCR capabilities; representative files still need fidelity tests.
- **R48** [Docker Compose](https://docs.docker.com/compose/) — Multi-container definition suitable for local and small single-host operation.
- **R49** [Keycloak OIDC](https://www.keycloak.org/securing-apps/oidc-layers) — OIDC endpoints for interoperable login.
- **R50** [React versions](https://react.dev/versions) — Use supported React 19 family; lock exact dependencies in implementation.
- **R51** [Vite requirements](https://vite.dev/guide/) — Check supported Node runtime at lock resolution.
- **R52** [Original RAG paper](https://arxiv.org/abs/2005.11401) — Retrieval-plus-generation basis, not a guarantee of enterprise grounding.
- **R53** [Lost in the Middle](https://arxiv.org/abs/2307.03172) — Context placement can affect use of evidence; evaluate packing.
- **R54** [Self-RAG](https://arxiv.org/abs/2310.11511) — Adaptive retrieval/critique research; not equivalent to a prompt-only retry loop.
- **R55** [Reciprocal rank fusion](https://cormack.uwaterloo.ca/cormacksigir09-rrf.pdf) — Rank-based result fusion avoids assuming dense and lexical score comparability.

Redis Streams documentation returned an internal retrieval error on three attempts. Redis is compared conceptually but no current Streams API claim is treated as verified; it is not a baseline dependency.

- **R56** [OpenAI embedding model](https://developers.openai.com/api/docs/models/text-embedding-3-small) — $0.02 per million input tokens observed.
- **R57** [OpenAI embedding guide](https://developers.openai.com/api/docs/guides/embeddings) — Small default1536 dimensions and8192 input limit in guide; verify account limits on enabling.

## v2 additional primary sources

- **R58** [Current Qdrant release](https://github.com/qdrant/qdrant/releases/tag/v1.19.2) — Stable release1.19.2 observed; patch fixes and features are not application conformance.
- **R59** [Qdrant hybrid queries](https://qdrant.tech/documentation/search/hybrid-queries/) — Query API supports dense/sparse and multistage retrieval.
- **R60** [Qdrant BM25](https://qdrant.tech/documentation/inference/inference-bm25/) — BM25 sparse encodings use Qdrant IDF; local FastEmbed is supported.
- **R61** [LangGraph persistence](https://docs.langchain.com/oss/python/langgraph/persistence) — Checkpointers store thread state; production persistence requires durable backend.
- **R62** [LangGraph interrupts](https://docs.langchain.com/oss/python/langgraph/interrupts) — Resume uses same thread; interrupted node restarts, so effects need idempotency.
- **R63** [Python downloads](https://www.python.org/downloads/) — 3.15.0 released Oct9;3.14.8 current preceding bugfix line.
- **R64** [Node release schedule](https://nodejs.org/en/about/previous-releases) — Node24 LTS and Node26 Current observed.
- **R65** [uv lock and sync](https://docs.astral.sh/uv/concepts/projects/sync/) — Locked sync checks lock currency; immutable installs require explicit lock policy.

Exact package releases have separate primary registry URLs and publication timestamps in [release inventory](00-research/releases-2026-10-10.json).

- **R66** [Qwen3.5-4B](https://huggingface.co/Qwen/Qwen3.5-4B) — Publisher model card; current challenger requires local artifact/conformance and paired evaluation.
- **R67** [Qwen3 embedding0.6B](https://huggingface.co/Qwen/Qwen3-Embedding-0.6B) — Publisher model card; current challenger requires local artifact/conformance and paired evaluation.
- **R68** [Qwen3 reranker0.6B](https://huggingface.co/Qwen/Qwen3-Reranker-0.6B) — Publisher model card; current challenger requires local artifact/conformance and paired evaluation.

- **R69** [Current Gemini model catalog](https://ai.google.dev/gemini-api/docs/models) — Stable3.8 Flash replaces disabled2.5 candidate; account probe still required.
