# Background workers and scoped maintenance

Run from the repository root after installing locked dependencies, applying migrations, building the parser image and preparing the pinned model artifacts. API uploads create durable jobs; these two separate processes consume them:

```bash
uv run --no-sync evidencedesk ingest-worker
uv run --no-sync evidencedesk index-worker
```

Run each command in its own process. `--once` processes at most one eligible claim and exits; it does not mean that the whole document is published. Ingestion can defer while the index worker verifies its outbox delivery, then reclaim its own job to publish. Both loops poll500ms when idle, sweep expired leases every10s, and handle termination by cancelling the current stage. Parser containers are removed; CPU inference retains its slot until its thread exits. Database fencing protects recovery after a forced kill.

Required environment for both workers:

| Variable | Purpose |
|---|---|
| `ED_DATABASE_URL` | Nonowner `ed_app` PostgreSQL DSN; tenant-scoped content operations |
| `ED_DISPATCH_DATABASE_URL` | Restricted `ed_dispatch` DSN; claims, renewal and recovery |
| `ED_S3_ENDPOINT`, `ED_S3_REGION`, `ED_S3_BUCKET` | Private object storage; region defaults to `us-east-1` |
| `ED_S3_ACCESS_KEY_FILE`, `ED_S3_SECRET_KEY_FILE` | Mounted credential files |
| `ED_INDEX_CONFIG_PATH` | Defaults to `contracts/config/qdrant.yaml` |

The ingestion worker also uses `ED_MODEL_CACHE_PATH` (default `.cache/models`), `ED_MODEL_ARTIFACT_LOCK` (default `contracts/config/model-artifacts.lock.json`), and `ED_PARSER_IMAGE_LOCK` (default `infra/parser/image.lock.json`). It verifies cached model/build hashes and never downloads on a job. It needs trusted operator access to the Docker engine to create the constrained parser container; the API and parser container must not receive that capability. It does not need Qdrant credentials.

The index worker additionally requires `ED_QDRANT_URL` and `ED_QDRANT_WRITE_KEY_FILE`. It provisions only the frozen operator collection layout. Qdrant uses a separate read-only credential for retrieval; neither key belongs in the browser. Worker settings omit OIDC/session and paid-model secrets.

Scoped operator audits:

```bash
uv run --no-sync evidencedesk index-reconcile --workspace WORKSPACE_UUID
uv run --no-sync evidencedesk index-reconcile --workspace WORKSPACE_UUID --after NEXT_REVISION_UUID
uv run --no-sync evidencedesk index-reconcile --workspace WORKSPACE_UUID --orphans
uv run --no-sync evidencedesk index-reconcile --workspace WORKSPACE_UUID --orphans --after NEXT_POINT_UUID
```

The first mode checks at most20 revisions and emits counts plus `next_revision`; drain that cursor to cover the workspace. The orphan mode checks at most100 points and emits `next_point`. A first sighting only quarantines an unknown point; removal requires an unchanged payload after24h and no authoritative chunk owner. Known IDs with corrupted tenant payloads are preserved for their owning manifest to repair. The audit command exits1 when it detects a failure, even when a repair was queued. Let the index worker deliver that repair, then audit again. Exhausted delivery attempts require explicit authorized retry/repair; periodic audits do not reset their budget.

### Checkpoint and replay retention

After migrations0020/0021, run these internal commands with `ED_DATABASE_URL` using the nonowner application role. Only `checkpoint-prune` also needs `ED_CHECKPOINT_DATABASE_URL`; neither needs dispatcher, identity, provider, object-store or Qdrant credentials.

```bash
uv run --no-sync evidencedesk checkpoint-prune --workspace WORKSPACE_UUID
uv run --no-sync evidencedesk checkpoint-prune --workspace WORKSPACE_UUID --after NEXT_THREAD_UUID
uv run --no-sync evidencedesk replay-prune --workspace WORKSPACE_UUID
```

Checkpoint output includes `checked`, `purged`, `busy`, `skipped` and `next_thread`. Drain nonnull cursors, then start subsequent sweeps without `--after` to revisit skipped threads. Only expired terminal workflows with no pending jobs/reservations qualify. Successful receipts fence later graph invocation; mappings remain for hourly reconciliation of delayed writes. Saver failure leaves the receipt unacknowledged and exits unsuccessfully; retrying is safe.

Replay output includes counts for `idempotency_keys`, `events`, `responses` and `may_have_more`. Each invocation handles at most100 rows per category in one transaction. Repeat when a category filled its batch. Rows locked by another writer are skipped, so `may_have_more=false` is not a guarantee that no expired row remains. Later sweeps must revisit them. Only terminal runs lose expired event history; saved messages and usage totals remain. Provider responses needed by pending recovery are retained.

These are bounded operator entrypoints. Fleet scheduling, physical content/object erasure and the full backup/restore drill remain T28/T30; running one page does not establish a retention SLA. See [ADR017](../../docs/adr/017-bounded-replay-and-checkpoint-retention.md).

### Source-derived answer redaction

```bash
uv run --no-sync evidencedesk source-redact --workspace WORKSPACE_UUID
```


This command requires only `ED_DATABASE_URL` with the application role. Each invocation processes one terminal run/page and reports `processed` and `may_have_more`. Repeat while `may_have_more=true` and schedule later sweeps for active runs that subsequently terminalize. Deleted-source answers and their private history descendants lose stored generated content, including cited titles, summaries, provider responses and saved briefs. Unrelated knowledge and independent conversations remain. User-authored questions and content-free usage totals remain.

Run normal checkpoint-retention sweeps afterward for the newly expired threads. This command does not delete original files, chunks or vector points. Future physical source purge must drain redaction while source rows still establish KB ownership. It is an internal operator entrypoint, not a new public API or automatic retention schedule. See [ADR019](../../docs/adr/019-source-derived-content-redaction.md).

### Conversation age retention

```sh
uv run --no-sync evidencedesk conversation-expire --workspace WORKSPACE_UUID
```

Uses only `ED_DATABASE_URL`. Each invocation tombstones up to25 conversations at least90 days old, measured from creation even if recently used, and queues their existing conversation purge jobs. It prints `expired` and `may_have_more`; repeat full pages and run `conversation-purge-worker` to perform erasure. Knowledge bases remain available. A creator's departure does not prevent expiry. All rows/jobs/tombstones in a page commit together. Fleet scheduling and a measured retention SLA are still pending; this command alone does not prove physical deletion.


### Source object cleanup

```bash
uv run --no-sync evidencedesk source-objects-purge --workspace WORKSPACE_UUID
```

Requires `ED_DATABASE_URL`, `ED_S3_ENDPOINT`, `ED_S3_BUCKET`, `ED_S3_ACCESS_KEY_FILE` and `ED_S3_SECRET_KEY_FILE`; optional `ED_S3_REGION` defaults to `us-east-1`. Use an unversioned bucket with GetBucketVersioning/ListBucket/DeleteObject permissions. Enabled or suspended versioning fails closed. No dispatcher or Qdrant credentials are needed. Each page processes one deleted document and at most100 objects, then reports `selected`, `removed`, `verified_empty`. Repeat until no document is selected, and schedule later sweeps to reconcile delayed writes after1h. PostgreSQL source rows and Qdrant points have separate cleanup; this does not mark parent purge jobs complete. See [ADR021](../../docs/adr/021-source-object-namespace-purge.md).

### Conversation physical purge

After migration0022, run:

```bash
uv run --no-sync evidencedesk conversation-purge-worker
# Process at most one bounded page, then exit:
uv run --no-sync evidencedesk conversation-purge-worker --once
```

Required settings are `ED_DATABASE_URL`, `ED_DISPATCH_DATABASE_URL` and `ED_CHECKPOINT_DATABASE_URL`, using the existing nonowner roles. No provider, object-store, Qdrant or identity credential is needed. A dedicated filtered dispatcher claim handles only conversation purge jobs; document/KB/workspace purge jobs are not consumed by this worker.

The worker rechecks the tombstone, deleted conversation, creator and lease on each page. It cancels/fences run jobs, settles unsettled usage without reissuing a model call, clears response bodies and removes checkpoints under their invocation gates. IDs are retained as standalone `workflow_thread` tombstones before removing mappings. Run children and cached replies are deleted in bounded pages; saved token/cost accounting remains with conversation/run references cleared. Shared knowledge-base files and indexes remain available to other conversations.

`--once` may leave a healthy job in `retry_wait`: one page is not the entire conversation. The daemon retries page progress/busy gates after1second without consuming the infrastructure retry budget. Saver/DB failure leaves the lease for recovery; three infrastructure failures can produce a dead job. Investigate dead jobs and stale tombstones. This command uses the existing fixed-field worker logs and never prints private content or underlying exception details.

Continue periodic `checkpoint-prune --workspace WORKSPACE_UUID` sweeps after physical deletion: it also reconciles standalone thread tombstones, removing writes that arrived late after gate loss. A missing parent workspace does not remove those tombstones. Do not manually drop them during cleanup; independent deletion-log retention/replay and its35-day policy remain part of unfinished disaster-recovery work. See [ADR018](../../docs/adr/018-conversation-purge-and-surviving-thread-tombstones.md).

T14 tests run actual worker subprocesses with disposable PostgreSQL/Qdrant and an HTTP S3 emulator, without browser-session or paid-provider credentials. Periodic fleet scheduling, retention/physical object purge, deployment supervision and the full restore-admission drill remain later lifecycle/infrastructure work. This is not yet a complete application startup recipe.

## Grounded chat worker (T17)

```bash
uv run --no-sync evidencedesk chat-worker
# One bounded queue claim, useful for isolated diagnostics:
uv run --no-sync evidencedesk chat-worker --once
```

Mount `ED_DATABASE_URL`, `ED_DISPATCH_DATABASE_URL`, `ED_IDENTITY_DATABASE_URL`, `ED_CHECKPOINT_DATABASE_URL`, `ED_QDRANT_URL` and `ED_QDRANT_READ_KEY_FILE`. The identity connection resolves current membership; it does not need OIDC client/session secrets. The index key must be read-only. No object-store credentials or parser/Docker socket belong in this worker.

The API and worker must share the same policy/model registry files (`ED_POLICY_CONFIG_PATH`, `ED_MODEL_CONFIG_PATH`) and immutable model/tokenizer artifacts (`ED_MODEL_CACHE_PATH`, `ED_MODEL_ARTIFACT_LOCK`, `ED_LOCAL_TOKENIZER_LOCK`). Configure `ED_LOCAL_MODEL_URL` plus optional `ED_LOCAL_MODEL_KEY_FILE`, and/or `ED_OPENAI_KEY_FILE`. Constructing a transport does not establish readiness: API admission and worker routing require a fresh successful provider_health report matching both model configuration and the SHA256 of the normalized operator endpoint. Existing reports without that endpoint binding fail closed and need a new probe. The worker never probes a paid model automatically. A report fabricated from a fixture is not live readiness.

The process warms BGE/BM25 before claiming work and starts the fixed offline MiniLM child under advisory slot3. Dead reranker children may warm again before a later claim, at most once per30s; the failed request is never retried. Startup warmup is bounded45s and is outside a job lease. The child receives no inherited application/provider secrets. One job runs per worker process; deployment admission remains5 global/2 per workspace, with single CPU slots for generation, embedding and reranking. Do not infer production throughput from the functional command test.

Grounded execution uses the existing60s original run deadline, heartbeat and LangGraph advisory gate. Frozen source/history references live in creator-private run metadata, raw provider responses in the private attempt ledger, and checkpoints contain IDs/hashes only. Provider JSON is buffered; only validated sentence text enters durable draft events. Final citations are built from current database passages. Interrupted/ambiguous attempts never automatically replay; the future T20 routing phase adds bounded definite-failure retry policy.

### Model readiness probes

`uv run --no-sync evidencedesk model-probe --model local_standard` runs one due synthetic probe. It requires `ED_DISPATCH_DATABASE_URL`, `ED_DATABASE_URL` (the global local-inference slot), model registry/tokenizer paths and the configured native endpoint. No browser, identity, object-store or Qdrant credentials are needed. It checks schema, deltas, terminal usage and client-side stream closure using two bounded160-output-token requests. A successful record is bound to the exact model configuration and endpoint; freshness expires after5minutes. Schedule this one-shot command every minute in the operator's process supervisor; fresh records skip transport. Half-open recovery has a single90-second token-fenced lease.

Hosted probes are opt-in: `uv run --no-sync evidencedesk model-probe --model paid_standard --allow-paid --max-cost-micro-usd 10000`. This command requires the mounted OpenAI key and can spend real money; implementation tests use scripted native transport only. The cap is per invocation, at most$0.01, and is checked against the conservative quote for both calls. A fleet-wide$1 UTC daily ceiling is reserved atomically in shared provider health before transport. Failed, interrupted and cancelled probes retain the full quote for the day. This is conservative operator overhead, separate from workspace/user usage reports. A restart cannot reclaim reservations. Schedule hosted probes only after deliberately accepting that recurring overhead; no serving worker silently enables them. An exhausted cap leaves the route unavailable after freshness expiry.

Cancellation capability means the adapter closed its client stream within3seconds. It does not prove remote compute or charging stopped. Readiness checks conformance, not answer quality. Never enter private corpus text in probe prompts.


### Approved brief worker

Run `uv run --no-sync evidencedesk brief-worker` (or `--once`) with `ED_DATABASE_URL`, `ED_DISPATCH_DATABASE_URL`, and `ED_CHECKPOINT_DATABASE_URL`. It needs no provider, identity, Qdrant or S3 credentials. Use the existing nonowner application/dispatcher/checkpoint roles. The process runs the lease sweeper every10seconds, resumes approved unfinished work, and expires parked proposals when due. Restarts converge on the unique execution artifact. Investigate dead jobs after three infrastructure failures; source/permission/hash failures are terminal. It never appends to a completed answer's SSE stream.
