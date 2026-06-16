# Backend implementation design

FastAPI route handlers do transport validation and call typed domain services; SQLAlchemy repositories own SQL; Pydantic types map public schemas. Service layer owns authorization and transactions. `apps/api/src/evidencedesk` is one package shared by API and worker entrypoints; no internal HTTP calls between domain modules. CPU parsing/embedding/reranking occur in bounded separate processes. No ORM object crosses process/adapter boundaries.

## Privileged bootstrap interfaces

Implement these SQL functions in migration T03–T05 with fixed search_path `pg_catalog,app`, schema-qualified objects, revoked PUBLIC EXECUTE. Owner is dedicated NOLOGIN role; runtime identity/dispatcher roles receive only EXECUTE, no arbitrary BYPASSRLS connection. Passing user ID is trusted only from verified session service, never user JSON. Roles and SQL function inputs must be covered by integration tests.

- `lookup_session(session_hash) -> user_id, expires_at, revoked_at, csrf_hash`: exact hash lookup, no user directory.
- `list_user_workspaces(actor_id, cursor, limit)`: join memberships to nondeleted workspaces where user_id=actor; return workspace metadata and role only.
- `get_membership(actor_id,workspace_id) -> role,version`: returns none for tombstone, no document data.
- `create_workspace(actor_id,id,name)`: quota check and workspace/owner rows plus initialized policies in one transaction; returns ID. Global idempotency handled in identity service transaction.
- `claim_job(kind,worker_id)`: select earliest eligible queued/retry_wait job `FOR UPDATE SKIP LOCKED LIMIT 1`, check attempts<3 and available_at<=now; set running, attempts+1, token+1, lease_until=now+30s; return workspace/job/actor/target/token only. Lease renew uses current token and unexpired lease. Revoked actor causes job cancel on domain preflight. Purge is operator-owned maintenance and may continue after user's removal.
- `sweep_expired_jobs(now)`: for ingest with attempts<3 enqueue retry_wait; for exhausted mark dead; for chat with any draft mark run interrupted and append one terminal event; without draft can requeue only if no issued/ambiguous attempt exists and still within the original60s deadline. A complete stored response may resume validation. An uncertain issued attempt interrupts even without a public draft.

Identity-scoped global idempotency (workspace creation/logout) needs a small `identity_idempotency_keys` table; separate from tenant keys and included in reference SQL. Global concurrent run admission needs a singleton `admission_counters` row locked before workspace/conversation; count current queued/running runs and reserve slot transactionally, recover by reconciliation on startup. Counter drift must fail closed when exhausted and self-reconcile, not permanently block.

## Job and index behavior

Claim transactions stay short; lease heartbeat uses a separate connection. Save checkpoint only after artifact hash verification. Stage checkpoints record stage input hash, result object keys, chunk count and profile versions. Duplicate worker write uses unique keys/upserts that confirm matching content; mismatched hash is integrity failure. A NOTIFY payload contains job ID only; dropped notifications recover through polling. Dispatchers batch no more than one job per worker slot to preserve fairness; round-robin workspace selection under backlog prevents one tenant monopolizing ingestion.

FastAPI lifespan warms configuration/DB pool, checks schema version and starts no embedded cron thread. Worker process handles scheduled sweeper tasks with singleton lease. API max DB pool10, worker pool5 each; local DB connection budget60 including maintenance and at most10 dedicated stream-reader connections; adjust only after load evidence. Set statement timeout3s request/30s maintenance, lock timeout1s; upload and model streams hold no DB transaction. Shutdown stops new admissions, drains API streams up to30s, releases/cancels worker leases appropriately.

## API/service failure classification

Validation and policy exceptions map to Problem responses. Database connection loss before commit is retryable at transport with idempotency key; after commit response ambiguity returns same result on replay. Blob upload failure returns503 before record creation; DB failure after blob creation leaves orphan swept later. Generation no-result is completed abstention; retrieval unavailable is failed dependency. Source deletion during run is source_revoked, not generic provider error. Cancellation/lease loss is not retried as a business call.

Acceptance tests use real PostgreSQL for constraints/locks/RLS, S3 emulator for object semantics and scripted provider streams for deterministic failures. Do not mock the very boundary being tested. Live provider smoke tests are opt-in, low-spend and never required for basic offline development.

## v2 ownership split

Job checkpoints describe ingestion artifacts; LangGraph saver describes answer/brief node progress. Index outbox owns Qdrant delivery leases. No duplicated schedulers for the same effect. Answer execution and brief proposal/resume use a server-owned thread gate and current AuthContext checks. The workflow protocol is normative for API-versus-worker concurrency and provider issued-state ambiguity. Proposal/approval endpoints acknowledge ToolExecution state; only succeeded+brief_id proves save. Public event log stays application-owned.
