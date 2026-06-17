# Failure recovery and degraded operation

| Failure | User-visible behavior | Recovery and invariant |
|---|---|---|
| IdP unavailable | Existing unexpired sessions work; new login fails with retry | Never bypass login or extend expired session |
| DB unavailable | Readiness503; no new admissions; active streams fail cleanly | No model call without reservation/persisted run; reconnect reads committed events |
| Object store unavailable | Upload/download unavailable; already indexed QA may work if source integrity remains known | Retrying same upload key does not create duplicate revision |
| Parser crash/time limit | Document failed with parse reason; old active revision remains | Transient infrastructure retry max3, malformed content permanent |
| Embedding unavailable | Ingestion waits/fails; chat lexical-only marked degraded | No incompatible embedding model swap |
| Reranker timeout | RRF result marked degraded | No queue amplification or repeated rerank call |
| Hosted provider outage | Eligible bounded fallback before draft; otherwise failed/interrupted | Privacy/capability/budget preserved; costs for all attempts |
| Worker killed | Expired lease reclaimed; chat draft interrupted | Fencing token blocks old worker commit; no fabricated continuation |
| SSE disconnect | Reconnect and replay | No automatic model re-invocation |
| Queue full |429 Retry-After before expensive work | Per-tenant fairness; ingestion cannot occupy chat worker slots |
| Telemetry sink unavailable | Serving continues; sampled logs/spans may drop | DB usage/audit commits remain mandatory |
| Disk nearly full | Pause upload at85%; at95% deny writes except maintenance | Alert, prune expired artifacts, expand disk; no delete of live sources |

Retry taxonomy: transient network/storage→full jitter exponential within deadline; invalid file/permission/schema/budget→no retry. DLQ is jobs.status=dead with safe error code, attempts, stage and artifact refs. Admin/editor retry only ingestion, resets generation and increments fencing token; retain audit. Operator fixes root cause before retrying bulk. Backpressure limits live queue and inference slots, with metrics for queue age and rejection rate.

Daily recovery job: scan expired leases, stale reservations, tombstoned objects, missing published manifests and orphan uploads. Reconcile budget reservation ambiguity as estimated usage, never forgive unknown provider spend automatically. All sweeps idempotent and bounded100 rows/transaction; resume by keyset cursor.

Backup: daily encrypted pg_dump (including schema/profile metadata) plus immutable object manifest and independent tombstone log copied off host. Backup object retention30d, tombstones35d. Keep encryption key outside dump location. A fresh-host restore drill: provision pinned images→restore DB→restore/verify referenced objects→replay deletion log→validate counts/checksums/RLS→run tenant smoke→enable traffic. Target RPO24h/RTO4h; measure timestamp evidence, never infer success from a completed backup command. Quarterly drill and before schema-destructive change.

Rollback: retain previous application image and backwards-compatible schema. On failed deployment, disable admissions, drain/cancel running workers, restore prior app image, preserve database unless migration rollback was rehearsed. Model/prompt/index rollback switches tested immutable versions; never reinterpret old vectors with new embedding model. If DB corruption requires restore, present downtime and possible ≤RPO data loss; do not serve partially reconciled tenants.

## v2 cross-store recovery

Qdrant unavailable: fail retrieval with retryable unavailable, no model call and no false empty-corpus abstention. A working sparse branch can degrade dense embedding failure; full Qdrant outage affects both. Checkpoint saver unavailable: stop graph advancement; retain committed application events and settle/estimate issued attempts. DB response persisted but checkpoint missing may resume validate/finalize; uncertain issued provider attempt never repeats automatically. Thread invocation gate prevents a second graph writer while old process holds it.

Include lg_checkpoints in encrypted PostgreSQL backup and thread deletion. Qdrant snapshots are optional acceleration; immutable vector artifacts+manifests rebuild derived state. During restore block admission, replay tombstones across every generation, reconstruct/verify all active manifests, purge expired checkpoints, then run tenant/compare/approval smoke. Do not bring search online merely because container health checks pass. See [index protocol](../05-database-and-storage/qdrant-index.md) and [workflow protocol](../03-ai-engineering/langgraph-workflows.md).
