# Product requirements: EvidenceDesk

Status: implementation in progress; individual acceptance evidence is tracked in `tasks/status.json` and `PROGRESS.md`. Owner: project maintainer. User confirmation: capstone must be feasible for one engineer, production-oriented and strong enough to highlight to AI recruiters. Acceptance criteria are release gates, not claims already achieved.

## Product contract

Members ask questions against workspace knowledge and inspect evidence. Editors curate sources and save reviewed briefs. Admins manage collections/settings and aggregate reports; owners additionally manage membership and workspace deletion. Conversations are private to their creator; documents are workspace-shared. One user can join several workspaces. No anonymous upload or public chat.

The product is an organization's persistent shared knowledge assistant. Editors populate the organization's knowledge bases once and maintain their document versions; members use that existing knowledge across separate conversations. Uploads belong to knowledge bases rather than being disposable attachments to a single chat. RAG is the answering mechanism. Automatic external-source connectors and a public customer-support widget are outside the currently implemented scope.

P0 is required for the deployable capstone; P1 is required for the full advanced showcase after baseline. P2 is stretch and cannot delay baseline. Defaults: English, 20 workspaces, 100 members total, 10k documents/100k active chunks deployment-wide, 1 GiB uploaded bytes per workspace, 5 concurrent generations total and 2 per workspace. These are admission limits, not proved capacity.

Every feature below inherits authenticated membership, UUID validation, standardized errors, request correlation, documented limits, and the API contract. Preconditions include the listed dependencies and appropriate current role. Outputs are committed records/API responses; asynchronous acceptance is not completion. Feature-specific acceptance is additional to security/contract tests.

## F01 — OIDC login and sessions (P0)

As a user I sign in without a project-specific password store.

- **Behavior, inputs and outputs:** Authorization code + PKCE via backend; callback validates state/nonce/issuer/audience; set opaque HttpOnly session; GET /v1/me returns identity; logout revokes session.
- **Preconditions/dependencies:** OIDC provider configuration.
- **Edges and failures:** Expired code, replay, disabled identity provider, clock skew ≤60s; safe return path only.
- **Security:** Secure SameSite=Lax cookie; CSRF header on mutations; no tokens in browser storage.
- **Acceptance:** Invalid/replayed callbacks rejected; logout invalidates next API request.
- **Owner/interface:** `identity`; `GET /v1/me`. Implement through T03; verify SEC-AUTH.

## F02 — Workspaces and membership (P0)

As an owner I create a workspace and manage known users.

- **Behavior, inputs and outputs:** Create workspace and owner membership atomically; list only memberships; add existing user by exact verified email; update role/remove membership; exactly one or more owners.
- **Preconditions/dependencies:** F01.
- **Edges and failures:** Last-owner removal/demotion 409; unknown email generic 404; workspace deletion asynchronously purges data.
- **Security:** Owner manages membership; no public user directory; all mutations audited; active requests recheck revocation.
- **Acceptance:** Two-tenant tests cannot enumerate or read foreign IDs; last owner invariant holds under race.
- **Owner/interface:** `tenancy`; `/v1/workspaces`. Implement through T04; verify SEC-TENANT.

## F03 — Knowledge bases (P0)

As an editor I organize documents into named collections.

- **Behavior, inputs and outputs:** Create/list KBs; admin deletes KB asynchronously; name 1–120 characters; all members read workspace KBs.
- **Preconditions/dependencies:** F02.
- **Edges and failures:** Duplicate names 409; deletion cancels jobs and tombstones sources immediately.
- **Security:** Editor writes; viewer reads; tenant composite foreign keys.
- **Acceptance:** Deletion makes every child unavailable before 202 is returned.
- **Owner/interface:** `knowledge`; `/v1/workspaces/{workspace_id}/knowledge-bases`. Implement through T10; verify INT-KB.

## F04 — Secure document upload (P0)

As an editor I upload supported knowledge with understandable validation.

- **Behavior, inputs and outputs:** POST multipart upload accepts one file ≤20 MiB, PDF/DOCX/MD/HTML/TXT/CSV/JSON; API stores opaque object, creates revision and queued job, returns 202.
- **Preconditions/dependencies:** F03.
- **Edges and failures:** Zero bytes, bad MIME/magic, encrypted PDF, zip bomb, malformed encoding fail with stable reason; identical file to same document/config returns existing revision.
- **Security:** No path interpolation or remote URL ingestion; parser network disabled; quota 1 GiB/workspace.
- **Acceptance:** Rejected files never searchable; accepted upload has revision/job IDs; 20 MiB+1 rejected 413.
- **Owner/interface:** `ingestion`; `POST /v1/workspaces/{workspace_id}/knowledge-bases/{kb_id}/documents`. Implement through T11; verify SEC-UPLOAD.

## F05 — Durable ingestion and revisions (P0)

As an editor I see progress and retry a failed source safely.

- **Behavior, inputs and outputs:** Poll job and document; validate→parse→normalize→chunk→dense+sparse embed→Qdrant delivery/verification→PostgreSQL pointer publication. Reprocess freezes profiles; previous active revision remains until complete manifest verification.
- **Preconditions/dependencies:** F04.
- **Edges and failures:** Crash after any stage reclaims lease; three attempts then dead; no partial publication; OCR-needed is explicit unsupported failure in core.
- **Security:** Current membership/write policy checked at enqueue; worker uses tenant scope and fencing; tombstone always wins.
- **Acceptance:** Kill/retry each ingestion and outbox boundary; staging points remain invisible; one active revision has exact verified point IDs/count/hashes.
- **Owner/interface:** `jobs`; `GET /v1/workspaces/{workspace_id}/jobs/{job_id}`. Implement through T12; verify CHAOS-INGEST.

## F06 — Conversations and history (P0)

As a member I revisit my own discussions.

- **Behavior, inputs and outputs:** Create/list/get/delete own conversations; append immutable user message with a run; paginated messages by sequence; display partial/failed answers distinctly.
- **Preconditions/dependencies:** F02.
- **Edges and failures:** One active run per conversation; conflict 409; deleting cancels run and removes history; same idempotency key cannot duplicate user message.
- **Security:** Conversation owner only, including workspace admins; metadata-only aggregate usage permitted.
- **Acceptance:** Concurrent submissions produce one active run; deleted history disappears and cannot be replayed.
- **Owner/interface:** `chat`; `/v1/workspaces/{workspace_id}/conversations`. Implement through T06; verify INT-CHAT.

## F07 — Grounded streaming answers (P0)

As a user I receive a sourced answer and know when evidence is insufficient.

- **Behavior, inputs and outputs:** Bounded LangGraph answer workflow; create run then authenticated SSE from committed PostgreSQL events. Final Answer has evidence refs and answered/abstained status; no evidence abstains without LLM; ambiguous issued attempts never replay automatically.
- **Preconditions/dependencies:** F05,F06.
- **Edges and failures:** Reconnect, cancel, refusal, malformed JSON, provider failure after text; draft display is unverified until completed.
- **Security:** Authorized published sources only; citations checked server-side; no untrusted HTML.
- **Acceptance:** Final citations resolve to supplied visible chunks; no failed draft is marked completed; absent evidence abstains.
- **Owner/interface:** `orchestrator`; `POST /v1/workspaces/{workspace_id}/conversations/{conversation_id}/runs`. Implement through T17; verify EVAL-GROUND.

## F08 — Evidence inspector (P0)

As a user I verify a claim in its original context.

- **Behavior, inputs and outputs:** Click citation opens chunk text with document/revision/page/heading; authorized original download is proxied as attachment.
- **Preconditions/dependencies:** F05,F07.
- **Edges and failures:** Old revision stays inspectable while document exists; deleted source shows unavailable and redacts quoted content.
- **Security:** Recheck membership/tombstone on every read; no public or long-lived object URLs.
- **Acceptance:** Every citation links to exact stored normalized passage and correct page metadata.
- **Owner/interface:** `evidence`; `GET /v1/workspaces/{workspace_id}/chunks/{chunk_id}`. Implement through T18; verify INT-CITATION.

## F09 — Providers and model selection (P0)

As a user I can choose a permitted paid or local model.

- **Behavior, inputs and outputs:** Catalog lists logical keys, capability/availability and privacy; auto or explicit key per run; core adapters OpenAI and Ollama; Anthropic/Google conformance mandatory before claiming support.
- **Preconditions/dependencies:** F06.
- **Edges and failures:** Missing credential disables candidate; unsupported schema or context rejects request; explicit selection never silently switches model.
- **Security:** Endpoint URLs/admin keys cannot come from prompt or user body; local-only workspace blocks all paid calls.
- **Acceptance:** Same fixtures validate both core adapters; local-only spy records zero external calls.
- **Owner/interface:** `providers`; `GET /v1/workspaces/{workspace_id}/models`. Implement through T07; verify CONTRACT-PROVIDER.

## F10 — Routing, budgets and usage (P0)

As an owner I control spend and inspect honest accounting.

- **Behavior, inputs and outputs:** Deterministic capability/privacy/budget filters; reserve worst-case cost in DB; settle each attempt; daily/monthly limits; GET usage groups by day/model.
- **Preconditions/dependencies:** F09.
- **Edges and failures:** Concurrent requests cannot over-reserve; unknown provider usage remains estimated; failure is not free; denied budget 429.
- **Security:** Owner/admin sees workspace aggregates; members own usage; no full prompts in reports.
- **Acceptance:** Race test stays within reservations; all retry/fallback costs counted; request cap $0.05.
- **Owner/interface:** `routing`; `GET /v1/workspaces/{workspace_id}/usage`. Implement through T20; verify INT-BUDGET.

## F11 — Retrieval controls (P0)

As an editor I compare retrieval choices without changing tenant policy.

- **Behavior, inputs and outputs:** Owner/admin selects dense/hybrid/hybrid_rerank profile and top_k1–8. Qdrant dense+sparse branches use server-authorized revision filters; PostgreSQL hydrates/rechecks candidate text; scope above4096 revisions returns422.
- **Preconditions/dependencies:** F05.
- **Edges and failures:** Empty KB, filters removing all candidates, reranker timeout, invalid profile; response records degradation.
- **Security:** Server-enforced scope; query rewrites cannot add KBs or tenant IDs.
- **Acceptance:** Identical frozen collection/corpus/config across variants; every branch excludes unauthorized/staging points; sparse IDF scope explicitly reported.
- **Owner/interface:** `retrieval`; `PATCH /v1/workspaces/{workspace_id}/settings`. Implement through T15; verify EVAL-RETRIEVAL.

## F12 — Document comparison and brief tool (P0)

As an engineer I compare two revisions and save a reviewed evidence brief.

- **Behavior, inputs and outputs:** LangGraph compares exactly two authorized revisions with bounded reads. Separate brief graph pauses for explicit payload-bound approval; REST decision returns approved/running until unique save succeeds.
- **Preconditions/dependencies:** F07,F08,F10.
- **Edges and failures:** Missing revision, conflicting statements, approval expiry, duplicate approval, revoked role, step exhaustion.
- **Security:** Two authorized revisions; no arbitrary tools; editor approval rechecked at execution; viewer cannot save.
- **Acceptance:** Restart at interrupt and duplicate approval produce one brief; changed hash/revoked sources/expired approval prevent save; completed answer stream never reopens.
- **Owner/interface:** `tools`; `POST /v1/workspaces/{workspace_id}/tool-executions/{execution_id}/approval`. Implement through T23; verify SEC-TOOL.

## F13 — Conversation memory (P1)

As a user I ask a follow-up within a bounded private history.

- **Behavior, inputs and outputs:** Last six completed turns prioritized with a 2048-token history budget; optional summary of older turns with provenance; no global personal memory.
- **Preconditions/dependencies:** F07.
- **Edges and failures:** Budget overflow drops oldest; deleted/tombstoned citations invalidate summary; summary failure falls back to recent turns.
- **Security:** Summary is untrusted context, creator-scoped, deleted with conversation.
- **Acceptance:** Pronoun follow-up preserves intent; planted historical instruction cannot change tool permissions.
- **Owner/interface:** `memory`; `ContextBuilder.build`. Implement through T21; verify EVAL-MEMORY.

## F14 — Feedback and quality review (P0)

As a user I flag an unhelpful or unsupported answer.

- **Behavior, inputs and outputs:** Upsert own message feedback vote ±1 and optional reason ≤1000 chars; no automatic training; admin sees aggregate counts.
- **Preconditions/dependencies:** F07.
- **Edges and failures:** Feedback on nonfinal/nonowned message 404/409; repeats replace vote.
- **Security:** No other user conversation exposed; opt-in export only with sanitized evidence.
- **Acceptance:** Vote round trips and deletion removes it; feedback alone cannot alter prompts/models.
- **Owner/interface:** `quality`; `PUT /v1/workspaces/{workspace_id}/messages/{message_id}/feedback`. Implement through T19; verify INT-FEEDBACK.

## F15 — Evaluation dashboard (P1)

As a maintainer I review versioned experiments before promoting a change.

- **Behavior, inputs and outputs:** Offline runner posts signed/admin-authenticated aggregate report and case metadata; list/get eval runs; metrics, dataset/config hash and hardware visible.
- **Preconditions/dependencies:** F11,F14.
- **Edges and failures:** Missing samples, timeouts and failed runs counted; no benchmark claim from smoke data.
- **Security:** Owner/admin; sanitized public corpus only for demo; artifact references cannot be external fetch instructions.
- **Acceptance:** Four retrieval variants and paid/local comparison reports reproducible from frozen manifests.
- **Owner/interface:** `evaluation`; `/v1/workspaces/{workspace_id}/evaluations`. Implement through T25; verify EVAL-REPORT.

## F16 — Administration and health (P0)

As an operator I identify unhealthy dependencies without leaking secrets.

- **Behavior, inputs and outputs:** Settings manage privacy/budgets/retrieval; process-only liveness; readiness checks PostgreSQL schema, Qdrant profile/index and checkpoint capability; admin health shows dependency/queue status.
- **Preconditions/dependencies:** F02.
- **Edges and failures:** Provider unavailable means degraded health, not API liveness failure; schema mismatch blocks readiness.
- **Security:** Public health contains no versions, credentials or tenant names; config changes audit actor/diff.
- **Acceptance:** Provider outage leaves source browsing operational; missing DB returns readiness 503.
- **Owner/interface:** `operations`; `GET /v1/workspaces/{workspace_id}/health`. Implement through T26; verify OPS-HEALTH.

## F17 — Deletion, retention and recovery (P0)

As an owner I remove data and understand backup retention.

- **Behavior, inputs and outputs:** Tombstone first fences reads/jobs; purge PG content, objects, every Qdrant generation and workflow checkpoints within24h. Retain conversations90d from creation (new messages do not extend expiry)/backups30d; restore applies deletion log and verifies/rebuilds indexes before admission.
- **Preconditions/dependencies:** F05,F06.
- **Edges and failures:** In-flight generation, offline worker, object delete failure, backup restore after deletion.
- **Security:** Audit contains IDs not content; document deletion invalidates cached evidence/summary; restored tenants remain offline until tombstones reconciled.
- **Acceptance:** Concurrent delete test prevents further content replay; restore exercise meets stated RPO/RTO.
- **Owner/interface:** `lifecycle`; `DELETE /v1/workspaces/{workspace_id}/documents/{document_id}`. Implement through T28; verify CHAOS-DELETE.

## F18 — Accessible frontend (P0)

As a keyboard or mobile user I can complete the main workflow.

- **Behavior, inputs and outputs:** Workspace/KB/chat/source/settings/usage/eval screens; adaptive panels; keyboard focus, screen-reader status, reduced-motion; no developer internals on main flow.
- **Preconditions/dependencies:** F01,F03,F07,F08.
- **Edges and failures:** Offline, interrupted stream, 401/403, pending ingestion and empty workspace have actionable states.
- **Security:** Escape all model output; Markdown links allow only safe schemes; no external image loads.
- **Acceptance:** Playwright upload→answer→citation→feedback passes desktop/mobile; automated a11y has no serious/critical findings.
- **Owner/interface:** `frontend`; `OpenAPI client + SSE protocol`. Implement through T09; verify UI-E2E.
