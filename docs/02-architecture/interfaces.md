# Internal interfaces and ownership

These typed signatures are pseudocode contracts, not application implementation. All IDs are UUIDs; datetimes are UTC. `AuthContext` is produced only by session middleware and workspace authorization, never deserialized from client/model arguments. It contains user_id, workspace_id, role, policy_revision and request_id. It is immutable.

```python
class DocumentService:
    async def upload(ctx: AuthContext, kb_id: UUID, blob: UploadStream,
                     idempotency_key: str) -> AcceptedDocument: ...
    async def reprocess(ctx, document_id, profile_id, expected_version) -> JobRef: ...
    async def delete(ctx, document_id) -> JobRef: ...

class Parser:
    def parse(local_path: str, limits: ParseLimits) -> list[NormalizedBlock]: ...
# NormalizedBlock: ordinal, text, kind(text/table/code), page_start/end or null,
# heading_path[str], source_locator, language, warnings[str], sha256.
# source_locator is deterministic parser provenance, not an executable URI.

class Chunker:
    def split(blocks, profile: ChunkProfile) -> list[ChunkDraft]: ...
# ChunkDraft: ordinal, parent_ordinal, text, embedding_text, text_sha256,
# token_count, block_start/end, char_start/end, page_start/end, heading_path.

class Embedder:
    async def embed_queries(texts: list[str], profile_id: str) -> EmbeddingBatch: ...
    async def embed_documents(texts: list[str], profile_id: str) -> EmbeddingBatch: ...
# Batch: vectors[float32], dimensions, normalized, model_revision, token_counts.
# Never silently truncate, reorder batches, accept NaN/zero vectors or change model.

class Retriever:
    async def retrieve(ctx, query: str, kb_ids: list[UUID],
                       profile: RetrievalProfile) -> RetrievalResult: ...
    async def retrieve_revisions(ctx, query: str, revision_ids: list[UUID],
                                 profile: RetrievalProfile) -> RetrievalResult: ...
# Exactly two revisions in compare mode, same workspace/authorized KB scope;
# published or superseded with live parent document; use explicit revision filter
# instead of active pointer, preserving every other authorization predicate.
# Result: evidence[Evidence], query_variants, branch_timings, degraded[str],
# active_revision_ids, index_generation, config_hash. Evidence includes chunk_id,
# doc/revision IDs, exact text, provenance, dense/lexical/rerank ranks and scores.

class ContextBuilder:
    async def build(ctx, conversation_id, query, evidence,
                    model: ModelConfig) -> PromptBundle: ...
# Bundle: ordered role messages, evidence allowlist, all token budgets,
# prompt_version, history_message_ids, summary_version, source_revision_ids.

class ModelAdapter:
    async def probe(config: ModelConfig) -> CapabilityReport: ...
    async def count_tokens(request: ModelRequest) -> TokenEstimate: ...
    async def stream(request: ModelRequest) -> AsyncIterator[ModelEvent]: ...
# Request: logical_key, messages, output_schema_id, max_output_tokens,
# timeout_ms, provider_options (allowlisted), cancellation token; no AuthContext.
# Events: started, text_delta, tool_call_delta, usage, completed, refused, failed.
# Usage identifies cumulative vs delta; normalized totals are cumulative once.

class Router:
    async def select(ctx, intent, preference, token_estimate) -> RoutePlan: ...
# Plan: ordered eligible keys <=2, reason_codes, pricing/config versions,
# estimated_cost, reservation_id. Explicit preference has exactly one key.

class ToolExecutor:
    async def propose(ctx, run_id, tool_name, arguments) -> ToolExecution: ...
    async def approve(ctx, execution_id, payload_hash, decision) -> ToolExecution: ...
    async def execute(ctx, execution_id, lease_token) -> ToolResult: ...
# Never accept tenant/user/endpoint/credential authority from tool arguments.

class JobRepository:
    async def claim(kind, worker_id) -> LeasedJob | None: ...
    async def checkpoint(job_id, lease_token, stage, artifact_refs) -> None: ...
    async def finish(job_id, lease_token, result) -> None: ...

class RunEventStore:
    async def append(run_id, lease_token, event) -> Sequence: ...
    async def read(ctx, run_id, after_seq, limit=100) -> list[RunEvent]: ...
# append checks lease, monotonically increments seq in same run row lock,
# and commits before stream readers can see it. Terminal event unique by run.
```

No adapter can query user data. The orchestrator supplies already-authorized context. No retriever may choose tenant policy. API and worker use the same domain authorization methods. Repository functions require context; cross-tenant maintenance has a separate narrowly privileged command, audited per workspace.

Typed failures: `ValidationError`, `NotFound`, `Forbidden`, `Conflict`, `BudgetExceeded`, `ProviderUnavailable`, `ProviderRefusal`, `DeadlineExceeded`, `Cancelled`, `LeaseLost`, `SourceRevoked`, `UnsupportedFormat`, `UnsafeFile`, `IntegrityMismatch`. Map to public stable codes in OpenAPI; sanitize provider details. Never retry permission errors, validation failures or budget denial. Unrecognized adapter event is a conformance error, not text to display.

Configuration has one authority: `contracts/config/defaults.yaml` for policies, `models.yaml` for model capabilities/prices, environment schema for infrastructure. Runtime resolves immutable config hash per run/revision. Updating config affects new work only, except membership/deletion/privacy revocations which override snapshots immediately.

## v2 search/workflow interfaces

```python
class IndexWriter:
    async def deliver(scope: VerifiedMaintenanceScope, operation_id: UUID,
                      lease_token: int) -> IndexDelivery: ...
    async def verify(scope, revision_id, profile_id) -> VerifiedManifest: ...
    async def reconcile(scope, limit: int = 100) -> ReconcileReport: ...
# Never infer authorization from point payload or outbox caller arguments.

class SearchIndex:
    async def query(scope: ResolvedScope, dense: list[float] | None,
                    sparse: SparseVector | None, exact: bool = False) -> CandidateLists: ...
# ResolvedScope contains workspace, permitted KB/revision IDs and concrete collection/profile,
# produced by authorized PostgreSQL lookup; no public unfiltered query method.

class WorkflowRunner:
    async def start_answer(ctx, run_id, leased_job) -> None: ...
    async def prepare_brief(ctx, execution_id) -> ToolExecution: ...
    async def resume_brief(ctx, execution_id) -> ToolExecution: ...
    async def delete_thread(scope, thread_id) -> None: ...
# Server resolves thread mapping, graph version and current authority. Resume arguments
# never include arbitrary checkpoint/graph commands. All invocations acquire thread gate.
```

`runs.execution_metadata` carries provider execution/recovery metadata, not a parallel custom graph checkpoint. `jobs.checkpoint` remains ingestion artifact progress. LangGraph's dedicated schema is owned by its pinned saver migrations. Business SQL/index outbox, model_usage and tool_executions determine whether work is already issued or complete.
