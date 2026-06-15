# Ecosystem research and decision evidence

Research completed before selection, 2026-10-10. **Fact** means the linked primary source states the capability. **Judgment** means a recommendation for this workload. **Hypothesis** means an experiment is required. Cost and latency comparisons below describe resource drivers, not measured superiority. Reference identifiers resolve in [references](../references.md).

## What a production knowledge assistant must demonstrate

The original [RAG paper](https://arxiv.org/abs/2005.11401) motivates retrieval-conditioned generation. It does not solve permissions, changing source versions, reliable citations, or operational recovery. [Lost in the Middle](https://arxiv.org/abs/2307.03172) gives a reason to measure evidence ordering rather than assume a large context solves retrieval. [Self-RAG](https://arxiv.org/abs/2310.11511) explores learned retrieval/critique: we will evaluate a bounded query transformation, without claiming to implement that training method.

The useful portfolio signal is a reproducible chain: authorized source → immutable revision → retrieved passage → supported answer → recorded cost → regression evidence. A general-purpose autonomous assistant makes those claims harder to verify. Fine-tuning is deferred until a labeled failure analysis shows retrieval/prompting cannot address a repeated problem.

## Orchestration options

| Option | Verified capability | Strength for this project | Cost/operational weakness | Decision |
|---|---|---|---|---|
| [LangGraph](https://docs.langchain.com/oss/python/langgraph/overview) | Stateful graph execution and durability | Useful for resumable branching, human checkpoints | Graph/checkpoint migrations and another state abstraction to learn/test | **Choose** StateGraph + PostgreSQL saver for bounded workflows and approval recovery |
| [LlamaIndex](https://developers.llamaindex.ai/python/framework/) | Retrieval/data and workflow framework | Fast connector exploration | Framework object model can obscure access filters, chunk provenance and version transitions | Experimental adapters only |
| [Haystack](https://docs.haystack.deepset.ai/docs/pipelines) | Explicit component pipelines | Clear retrieval experimentation | Another pipeline schema and serialization surface | Good alternative if retrieval experimentation dominates product work |
| Custom typed Python services | Project-owned deterministic state machine | All policy and failure behavior visible; easy unit isolation | Must implement bounded job leases and stream replay carefully | Keep typed policy/domain services inside LangGraph nodes; no custom graph scheduler |

LangGraph is the selected orchestrator in v2. Explicit domain contracts preserve authorization, billing and event ownership; checkpoints handle node progress. See the detailed workflow protocol rather than assuming framework persistence supplies exactly-once effects.

## Backend and frontend

[FastAPI](https://fastapi.tiangolo.com/async/) supports async request handling; CPU extraction/inference still belongs outside the API event loop. Choose FastAPI/Pydantic/SQLAlchemy/Alembic so retrieval and APIs share Python types. [Django REST Framework](https://www.django-rest-framework.org/) offers a strong CRUD/admin ecosystem, but its main advantage is less central here. [NestJS](https://docs.nestjs.com/) makes sense for a TypeScript team; here Python inference adds a second backend boundary. No benchmark-based speed claims are made.

Choose React 19 + TypeScript + Vite SPA. Next.js is a viable SSR product stack, but this authenticated workspace has no SEO requirement; a second application server adds little. TanStack Query manages server state; reducer/local state manages the active stream. No global Redux store for duplicated API records. Use accessible headless components and CSS tokens; screenshots come from implementation, not fabricated mock product results.

## Retrieval and storage alternatives

| Option | Strength | Weakness/resource driver | Project recommendation |
|---|---|---|---|
| [PostgreSQL + pgvector](https://github.com/pgvector/pgvector) | Metadata, vectors and publication transaction share one DB | ANN filtering can under-return; vector working set competes with OLTP | Valid simpler alternative; superseded baseline in v2 |
| [Qdrant](https://qdrant.tech/documentation/manage-data/multitenancy/) | Purpose-built vector filtering and tenant strategies | Second durability/backup boundary; metadata publication needs reconciliation | **Choose** derived dense+sparse index with verified publication and reconciliation |
| [Weaviate](https://docs.weaviate.io/weaviate/manage-collections/multi-tenancy) | Tenant lifecycle and integrated retrieval features | Another schema/API and service to operate | Reasonable alternative, no feature need justifies migration initially |
| [Milvus](https://milvus.io/docs/multi_tenancy.md) | Multiple isolation/scale strategies | Distributed deployment is disproportionate for one engineer's demo | Revisit with measured large vector workload |
| [OpenSearch](https://docs.opensearch.org/latest/vector-search/ai-search/hybrid-search/index/) | Neural/lexical hybrid pipelines | JVM/search operations and duplicate indexing | Alternative lexical engine; Qdrant sparse BM25 is core |
| [Elasticsearch](https://www.elastic.co/docs/reference/elasticsearch/index-settings/similarity) | BM25 and rich lexical search | Additional service, licensing/hosting review | Comparator to OpenSearch if organization already uses it |

Baseline lexical retrieval is FastEmbed BM25 sparse vectors with Qdrant IDF. Use [RRF](https://cormack.uwaterloo.ca/cormacksigir09-rrf.pdf) to fuse rank lists. Do not sum incomparable cosine and lexical scores. Use sparse-only as lexical comparator. Collection-wide IDF and fixed encoder average length are declared experimental conditions. BGE dense + sparse BM25 + MiniLM is a hypothesis, not a proven winning combination.

## Queue, caching and coordination

PostgreSQL [SKIP LOCKED](https://www.postgresql.org/docs/18/sql-select.html) supports queue consumers. Choose job rows, leases, fencing tokens and a sweeper; poll at 500 ms with NOTIFY as an optional wake-up hint. The database is already required and transactions atomically create business records and jobs. At-least-once execution is explicit.

Redis is valuable for shared rate counters, ephemeral cache and low-latency coordination, but eviction/durability/configuration become additional concerns. A broker such as [Celery](https://docs.celeryq.dev/en/stable/userguide/tasks.html) provides richer task machinery but still needs idempotent effects. Neither is necessary for initial capacity. Trigger review if queue polling exceeds 10% database CPU or admission latency p95 exceeds 200 ms after tuning. Do not claim those thresholds are vendor limits.

## Model serving and providers

| Option | Integration and practical advantage | Limitation | Role |
|---|---|---|---|
| OpenAI Responses | [Typed streams](https://developers.openai.com/api/docs/guides/streaming-responses), [structured output](https://developers.openai.com/api/docs/guides/structured-outputs) | Schema subset; refusal/truncation still handled; network/data policy | First paid adapter |
| Anthropic Messages | [Structured output controls](https://platform.claude.com/docs/en/build-with-claude/structured-outputs) and tool use | Different content blocks, usage and thinking behavior | Second paid adapter, hardening stage |
| Google Gemini | [Structured output](https://ai.google.dev/gemini-api/docs/structured-output), several price tiers | Candidate/block finish reasons and thinking token accounting differ | Third paid adapter, advanced stage |
| [vLLM](https://docs.vllm.ai/en/latest/serving/online_serving/openai_compatible_server/) | GPU serving with compatible HTTP API | Template/tool parser/quantization support depends on model and version | Optional GPU deployment for the same local adapter conformance suite |
| [Ollama](https://docs.ollama.com/api/openai-compatibility) | Straightforward local model operation | Partial compatibility; CPU latency varies; context consumes RAM | First open-weight runtime |
| [llama.cpp](https://github.com/ggml-org/llama.cpp) | Quantized CPU/GPU inference and portable server | Build/backend/quantization tuning burden | Alternative if Ollama overhead/control becomes limiting |

Native provider adapters are intentionally small; a gateway such as LiteLLM is deferred to avoid hiding capability mismatches. Unsupported options are rejected, never silently dropped.

Open-weight candidates: [Qwen3-8B](https://huggingface.co/Qwen/Qwen3-8B) is the default evaluation candidate for its manageable size and documented non-thinking mode; [Llama-3.1-8B-Instruct](https://huggingface.co/meta-llama/Llama-3.1-8B-Instruct) is a comparable-size alternative with different license obligations; [Mistral Small 3.1 24B](https://huggingface.co/mistralai/Mistral-Small-3.1-24B-Instruct-2503) demands more memory. No claim that Qwen wins this corpus is warranted before evaluation.

Paid default candidate: [GPT-6 Luna](https://developers.openai.com/api/docs/models/gpt-6-luna), no-reasoning option subject to probe. GPT-6.1 Sol is the quality challenger and GPT-4.1 mini a disabled dated reference. Activation requires conformance and held-out evaluation; a current alias is not an immutable snapshot. See current-stack.md and the registry.

## Embedding and reranking choices

[BGE-small-en-v1.5](https://huggingface.co/BAAI/bge-small-en-v1.5) offers 384-dimensional vectors with a 512-token limit. Choose normalized float32 local inference and smaller chunks to make CPU development practical. [BGE-M3](https://huggingface.co/BAAI/bge-m3) is a multilingual alternative, requiring a separate embedding generation and evaluation. Paid embeddings offer managed throughput but incur egress/privacy and usage costs; never mix their vectors with BGE. [MiniLM L6 cross-encoder](https://huggingface.co/cross-encoder/ms-marco-MiniLM-L6-v2) is a bounded CPU candidate; logit scores are not calibrated confidence.

## Evaluation and telemetry

| Option | Strength | Limitation | Choice |
|---|---|---|---|
| [Ragas](https://docs.ragas.io/en/stable/concepts/metrics/available_metrics/) | RAG-specific judge metrics | Judge variance/cost; cannot replace human labels | Optional offline supplemental scoring |
| [DeepEval](https://deepeval.com/docs/getting-started) | Test-oriented evaluation | Framework coupling and overlapping metric semantics | Compare in spike, do not install alongside everything |
| [Promptfoo](https://www.promptfoo.dev/docs/intro/) | Configurable prompt/provider matrices | Requires adapter and separate runtime for Python domain checks | Optional prompt regression runner |
| Custom pytest + JSONL harness | Exact retrieval/permission/citation metrics tied to contracts | Must define statistics and datasets | **Choose** primary harness |
| [OpenTelemetry](https://opentelemetry.io/docs/specs/semconv/gen-ai/) | Portable distributed instrumentation | GenAI conventions change | **Choose** tracing + Prometheus; pinned app metric names |
| [Langfuse](https://langfuse.com/self-hosting) | LLM trace/eval inspection | Full self-host increases footprint; prompt capture can leak content | Optional managed/redacted integration, not baseline self-host |

## Infrastructure and security findings

[Compose](https://docs.docker.com/compose/) is enough for a single-host demo; production reference separates DB, stateless APIs, workers and inference. Kubernetes is justified only when scheduling/HA requirements outweigh its operations burden. The [OWASP injection](https://cheatsheetseries.owasp.org/cheatsheets/LLM_Prompt_Injection_Prevention_Cheat_Sheet.html) and [file upload](https://cheatsheetseries.owasp.org/cheatsheets/File_Upload_Cheat_Sheet.html) guidance motivate enforceable boundaries: tenant-scoped queries, allowlisted tools, approval for saved artifacts, bounded isolated parsers. These controls are design recommendations, not a certification.

## Verification limits

Sources establish documented capabilities, not integration compatibility, model availability on the owner's account, expected throughput or local hardware fit. Latest release labels are mutable. Phase 1 must resolve exact patches and image digests, audit licenses, and exercise a small parse→embed→retrieve→generate spike. No installation, inference, paid API call, benchmark or deployment was performed during research.

## v2 revision evidence

[Current stack and release inventory](current-stack.md) supersedes the original version selection. Qdrant and LangGraph are selected core technologies by user direction; their costs and recovery obligations are addressed in ADR-012/013. Current package metadata does not prove a compatible lock. Documentation contract probes are reported separately from still-pending implementation T01.
