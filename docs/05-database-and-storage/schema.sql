-- EvidenceDesk v2 reference business schema. PostgreSQL18; Qdrant vectors and vendor-owned LangGraph checkpoint schema are separate.
-- DDL artifact, not a deployed migration. Role grants and tenant context are specified
-- in storage-design.md; never run the application as table owner/superuser.
BEGIN;
CREATE SCHEMA IF NOT EXISTS app;
CREATE TABLE app.users (
 id uuid PRIMARY KEY, issuer text NOT NULL, subject text NOT NULL,
 email text NOT NULL, display_name text NOT NULL, created_at timestamptz NOT NULL DEFAULT now(),
 UNIQUE(issuer,subject), UNIQUE(email)
);
CREATE TABLE app.sessions (
 id_hash text PRIMARY KEY CHECK(length(id_hash)=64), user_id uuid NOT NULL REFERENCES app.users,
 csrf_hash text NOT NULL, last_seen_at timestamptz NOT NULL DEFAULT now(), expires_at timestamptz NOT NULL, revoked_at timestamptz,
 created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX sessions_expiry ON app.sessions(expires_at);
CREATE TABLE app.login_states (
 state_hash text PRIMARY KEY, nonce_hash text NOT NULL, pkce_verifier_ciphertext bytea NOT NULL,
 return_path text NOT NULL, expires_at timestamptz NOT NULL, consumed_at timestamptz
);
CREATE TABLE app.identity_idempotency_keys (
 user_id uuid NOT NULL REFERENCES app.users, method_path text NOT NULL, key_hash text NOT NULL,
 request_sha256 text NOT NULL, response_status int, response_body jsonb, expires_at timestamptz NOT NULL,
 PRIMARY KEY(user_id,method_path,key_hash)
);
CREATE TABLE app.admission_counters (
 singleton boolean PRIMARY KEY DEFAULT true CHECK(singleton), active_runs int NOT NULL DEFAULT 0 CHECK(active_runs>=0),
 updated_at timestamptz NOT NULL DEFAULT now()
);
INSERT INTO app.admission_counters(singleton) VALUES(true);
CREATE TABLE app.workspaces (
 id uuid PRIMARY KEY, name text NOT NULL CHECK(length(name) BETWEEN 1 AND 120),
 privacy_mode text NOT NULL DEFAULT 'hosted_allowed' CHECK(privacy_mode IN ('hosted_allowed','local_only')),
 daily_budget_micro_usd bigint NOT NULL DEFAULT 2000000 CHECK(daily_budget_micro_usd>=0),
 monthly_budget_micro_usd bigint NOT NULL DEFAULT 20000000 CHECK(monthly_budget_micro_usd>=0),
 retrieval_profile text NOT NULL DEFAULT 'hybrid_rerank' CHECK(retrieval_profile IN ('dense','hybrid','hybrid_rerank','hybrid_rewrite')),
 top_k int NOT NULL DEFAULT 8 CHECK(top_k BETWEEN 1 AND 8), summaries_enabled boolean NOT NULL DEFAULT false,
 embedding_profile text NOT NULL DEFAULT 'bge_small_en_v15', corpus_generation bigint NOT NULL DEFAULT 1,
 version bigint NOT NULL DEFAULT 1, created_at timestamptz NOT NULL DEFAULT now(), deleted_at timestamptz
);
CREATE TABLE app.memberships (
 workspace_id uuid NOT NULL REFERENCES app.workspaces ON DELETE CASCADE,
 user_id uuid NOT NULL REFERENCES app.users, role text NOT NULL CHECK(role IN ('viewer','editor','admin','owner')),
 version bigint NOT NULL DEFAULT 1, created_at timestamptz NOT NULL DEFAULT now(), PRIMARY KEY(workspace_id,user_id)
);
CREATE INDEX membership_user ON app.memberships(user_id,workspace_id);
CREATE TABLE app.knowledge_bases (
 workspace_id uuid NOT NULL REFERENCES app.workspaces ON DELETE CASCADE, id uuid NOT NULL,
 name text NOT NULL CHECK(length(name) BETWEEN 1 AND 120), created_by uuid NOT NULL REFERENCES app.users,
 created_at timestamptz NOT NULL DEFAULT now(), deleted_at timestamptz, PRIMARY KEY(workspace_id,id)
);
CREATE UNIQUE INDEX kb_live_name ON app.knowledge_bases(workspace_id,lower(name)) WHERE deleted_at IS NULL;
CREATE TABLE app.documents (
 workspace_id uuid NOT NULL, id uuid NOT NULL, kb_id uuid NOT NULL,
 title text NOT NULL CHECK(length(title)<=255), created_by uuid NOT NULL REFERENCES app.users,
 active_revision_id uuid, latest_revision_id uuid, version bigint NOT NULL DEFAULT 1,
 created_at timestamptz NOT NULL DEFAULT now(), deleted_at timestamptz,
 PRIMARY KEY(workspace_id,id), UNIQUE(workspace_id,id,kb_id),
 FOREIGN KEY(workspace_id,kb_id) REFERENCES app.knowledge_bases(workspace_id,id) ON DELETE CASCADE
);
CREATE INDEX documents_kb ON app.documents(workspace_id,kb_id,created_at,id) WHERE deleted_at IS NULL;
CREATE TABLE app.document_revisions (
 workspace_id uuid NOT NULL, id uuid NOT NULL, document_id uuid NOT NULL, kb_id uuid NOT NULL,
 revision_no int NOT NULL CHECK(revision_no>0),
 status text NOT NULL CHECK(status IN ('queued','validating','parsing','normalizing','chunking','embedding','verifying','published','superseded','retry_wait','failed','cancelled','deleting','deleted')),
 object_key text NOT NULL, mime_type text NOT NULL, byte_size bigint NOT NULL CHECK(byte_size BETWEEN 1 AND 20971520),
 content_sha256 text NOT NULL CHECK(content_sha256 ~ '^[a-f0-9]{64}$'),
 parser_version text NOT NULL, chunk_profile_id text NOT NULL, profile_hash text NOT NULL,
 embedding_profile_id text NOT NULL, chunk_count int NOT NULL DEFAULT 0 CHECK(chunk_count>=0),
 manifest_key text, warnings jsonb NOT NULL DEFAULT '[]' CHECK(jsonb_typeof(warnings)='array'),
 created_at timestamptz NOT NULL DEFAULT now(), published_at timestamptz,
 PRIMARY KEY(workspace_id,id), UNIQUE(workspace_id,document_id,revision_no),
 UNIQUE(workspace_id,id,document_id,kb_id), UNIQUE(workspace_id,id,document_id),
 FOREIGN KEY(workspace_id,document_id,kb_id) REFERENCES app.documents(workspace_id,id,kb_id) ON DELETE CASCADE
);
ALTER TABLE app.documents ADD CONSTRAINT active_revision_fk
 FOREIGN KEY(workspace_id,active_revision_id,id) REFERENCES app.document_revisions(workspace_id,id,document_id)
 DEFERRABLE INITIALLY DEFERRED;
ALTER TABLE app.documents ADD CONSTRAINT latest_revision_fk
 FOREIGN KEY(workspace_id,latest_revision_id,id) REFERENCES app.document_revisions(workspace_id,id,document_id)
 DEFERRABLE INITIALLY DEFERRED;
CREATE TABLE app.embedding_profiles (
 id text PRIMARY KEY, model_id text NOT NULL, model_revision text NOT NULL, tokenizer_revision text NOT NULL,
 dimensions int NOT NULL CHECK(dimensions>0), normalized boolean NOT NULL,
 config_sha256 text NOT NULL, created_at timestamptz NOT NULL DEFAULT now()
);
ALTER TABLE app.workspaces ADD FOREIGN KEY(embedding_profile) REFERENCES app.embedding_profiles(id) DEFERRABLE INITIALLY DEFERRED;
ALTER TABLE app.document_revisions ADD FOREIGN KEY(embedding_profile_id) REFERENCES app.embedding_profiles(id);
CREATE TABLE app.chunks (
 workspace_id uuid NOT NULL, id uuid NOT NULL, document_id uuid NOT NULL, revision_id uuid NOT NULL, kb_id uuid NOT NULL,
 ordinal int NOT NULL CHECK(ordinal>=0), parent_ordinal int, text text NOT NULL, embedding_text text NOT NULL,
 text_sha256 text NOT NULL CHECK(text_sha256 ~ '^[a-f0-9]{64}$'), token_count int NOT NULL CHECK(token_count BETWEEN 1 AND 384),
 char_start int NOT NULL CHECK(char_start>=0), char_end int NOT NULL CHECK(char_end>char_start),
 page_start int CHECK(page_start>=1), page_end int CHECK(page_end>=page_start),
 heading_path jsonb NOT NULL DEFAULT '[]' CHECK(jsonb_typeof(heading_path)='array'),
 source_locator jsonb NOT NULL CHECK(jsonb_typeof(source_locator)='object'),
 PRIMARY KEY(workspace_id,id), UNIQUE(workspace_id,revision_id,ordinal),
 FOREIGN KEY(workspace_id,revision_id,document_id,kb_id)
  REFERENCES app.document_revisions(workspace_id,id,document_id,kb_id) ON DELETE CASCADE
);
CREATE INDEX chunks_revision ON app.chunks(workspace_id,revision_id);
CREATE UNIQUE INDEX chunks_global_id ON app.chunks(id);
CREATE TABLE app.index_manifests (
 workspace_id uuid NOT NULL, revision_id uuid NOT NULL, profile_id text NOT NULL REFERENCES app.embedding_profiles,
 collection_name text NOT NULL, state text NOT NULL CHECK(state IN ('staged','indexing','verified','unavailable','purging','purged','failed')),
 expected_points int NOT NULL CHECK(expected_points>=0), verified_points int NOT NULL DEFAULT 0 CHECK(verified_points>=0),
 manifest_sha256 text NOT NULL CHECK(manifest_sha256 ~ '^[a-f0-9]{64}$'), artifact_key text NOT NULL,
 verified_at timestamptz, updated_at timestamptz NOT NULL DEFAULT now(),
 PRIMARY KEY(workspace_id,revision_id,profile_id),
 FOREIGN KEY(workspace_id,revision_id) REFERENCES app.document_revisions(workspace_id,id) ON DELETE CASCADE,
 CHECK(state <> 'verified' OR (verified_points=expected_points AND expected_points>0 AND verified_at IS NOT NULL))
);
CREATE TABLE app.index_outbox (
 workspace_id uuid NOT NULL, id uuid NOT NULL, revision_id uuid NOT NULL, profile_id text NOT NULL,
 operation text NOT NULL CHECK(operation IN ('upsert','purge')), operation_key text NOT NULL,
 state text NOT NULL DEFAULT 'pending' CHECK(state IN ('pending','leased','done','dead')),
 attempts int NOT NULL DEFAULT 0 CHECK(attempts>=0), lease_token bigint NOT NULL DEFAULT 0,
 lease_until timestamptz, next_attempt_at timestamptz NOT NULL DEFAULT now(), error_code text,
 created_at timestamptz NOT NULL DEFAULT now(), PRIMARY KEY(workspace_id,id), UNIQUE(workspace_id,operation_key),
 FOREIGN KEY(workspace_id,revision_id,profile_id) REFERENCES app.index_manifests(workspace_id,revision_id,profile_id) ON DELETE CASCADE
);
CREATE INDEX index_outbox_pending ON app.index_outbox(next_attempt_at) WHERE state IN ('pending','leased');
CREATE TABLE app.index_orphans (
 workspace_id uuid NOT NULL REFERENCES app.workspaces ON DELETE CASCADE,
 collection_name text NOT NULL, point_id text NOT NULL CHECK(length(point_id) BETWEEN 1 AND 128),
 payload_sha256 text NOT NULL CHECK(payload_sha256 ~ '^[a-f0-9]{64}$'),
 first_seen timestamptz NOT NULL DEFAULT now(), last_seen timestamptz NOT NULL DEFAULT now(),
 PRIMARY KEY(workspace_id,collection_name,point_id)
);
CREATE TABLE app.conversations (
 workspace_id uuid NOT NULL REFERENCES app.workspaces ON DELETE CASCADE, id uuid NOT NULL,
 user_id uuid NOT NULL REFERENCES app.users, title text NOT NULL CHECK(length(title)<=120),
 version bigint NOT NULL DEFAULT 1, next_sequence bigint NOT NULL DEFAULT 1,
 summary jsonb, summary_through_seq bigint, summary_version text,
 created_at timestamptz NOT NULL DEFAULT now(), updated_at timestamptz NOT NULL DEFAULT now(), deleted_at timestamptz,
 PRIMARY KEY(workspace_id,id), UNIQUE(workspace_id,id,user_id)
);
CREATE INDEX conversations_user ON app.conversations(workspace_id,user_id,updated_at DESC,id) WHERE deleted_at IS NULL;
CREATE TABLE app.messages (
 workspace_id uuid NOT NULL, id uuid NOT NULL, conversation_id uuid NOT NULL, user_id uuid NOT NULL,
 sequence bigint NOT NULL CHECK(sequence>0), role text NOT NULL CHECK(role IN ('user','assistant')),
 state text NOT NULL CHECK(state IN ('final','draft','failed','interrupted','cancelled','refused')),
 text text NOT NULL DEFAULT '', answer jsonb, citations jsonb NOT NULL DEFAULT '[]',
 created_at timestamptz NOT NULL DEFAULT now(), PRIMARY KEY(workspace_id,id),
 UNIQUE(workspace_id,conversation_id,sequence), UNIQUE(workspace_id,id,user_id),
 UNIQUE(workspace_id,id,conversation_id),
 FOREIGN KEY(workspace_id,conversation_id,user_id) REFERENCES app.conversations(workspace_id,id,user_id) ON DELETE CASCADE
);
CREATE TABLE app.runs (
 workspace_id uuid NOT NULL, id uuid NOT NULL, conversation_id uuid NOT NULL, user_id uuid NOT NULL,
 user_message_id uuid NOT NULL, assistant_message_id uuid,
 status text NOT NULL CHECK(status IN ('queued','running','completed','failed','interrupted','cancelled','refused')),
 intent text NOT NULL CHECK(intent IN ('ask','compare')), model_key text, requested_model_key text NOT NULL,
 phase text NOT NULL DEFAULT 'queued', config_sha256 text NOT NULL, prompt_version text NOT NULL,
 execution_metadata jsonb NOT NULL DEFAULT '{}', evidence jsonb NOT NULL DEFAULT '[]', degraded jsonb NOT NULL DEFAULT '[]',
 cancel_requested boolean NOT NULL DEFAULT false, next_event_seq bigint NOT NULL DEFAULT 1,
 error_code text, created_at timestamptz NOT NULL DEFAULT now(), completed_at timestamptz,
 PRIMARY KEY(workspace_id,id), UNIQUE(workspace_id,id,user_id),
 FOREIGN KEY(workspace_id,conversation_id,user_id) REFERENCES app.conversations(workspace_id,id,user_id) ON DELETE CASCADE,
 FOREIGN KEY(workspace_id,user_message_id,conversation_id) REFERENCES app.messages(workspace_id,id,conversation_id),
 FOREIGN KEY(workspace_id,assistant_message_id,conversation_id) REFERENCES app.messages(workspace_id,id,conversation_id)
);
CREATE UNIQUE INDEX one_active_run ON app.runs(workspace_id,conversation_id) WHERE status IN ('queued','running');
CREATE INDEX run_retention ON app.runs(workspace_id,created_at,id) WHERE status NOT IN ('queued','running');
CREATE INDEX conversation_runs ON app.runs(workspace_id,conversation_id,id);
CREATE TABLE app.run_events (
 workspace_id uuid NOT NULL, run_id uuid NOT NULL, sequence bigint NOT NULL CHECK(sequence>0),
 type text NOT NULL CHECK(type IN ('run.accepted','run.phase','answer.delta','answer.completed','run.failed','run.cancelled','run.refused')),
 data jsonb NOT NULL, occurred_at timestamptz NOT NULL DEFAULT now(), PRIMARY KEY(workspace_id,run_id,sequence),
 FOREIGN KEY(workspace_id,run_id) REFERENCES app.runs(workspace_id,id) ON DELETE CASCADE
);
CREATE UNIQUE INDEX one_terminal_event ON app.run_events(workspace_id,run_id)
 WHERE type IN ('answer.completed','run.failed','run.cancelled','run.refused');
CREATE TABLE app.jobs (
 workspace_id uuid NOT NULL REFERENCES app.workspaces ON DELETE CASCADE, id uuid NOT NULL,
 kind text NOT NULL CHECK(kind IN ('ingest','chat','purge','summary','maintenance','workflow')),
 status text NOT NULL CHECK(status IN ('queued','running','retry_wait','succeeded','dead','cancelled')),
 run_id uuid, revision_id uuid, requested_by uuid NOT NULL REFERENCES app.users,
 payload jsonb NOT NULL DEFAULT '{}', checkpoint jsonb NOT NULL DEFAULT '{}',
 attempts int NOT NULL DEFAULT 0 CHECK(attempts BETWEEN 0 AND 3), generation int NOT NULL DEFAULT 1,
 available_at timestamptz NOT NULL DEFAULT now(), lease_until timestamptz, lease_token bigint NOT NULL DEFAULT 0,
 worker_id text, dedupe_key text NOT NULL, error_code text, progress int NOT NULL DEFAULT 0 CHECK(progress BETWEEN 0 AND 100),
 created_at timestamptz NOT NULL DEFAULT now(), updated_at timestamptz NOT NULL DEFAULT now(),
 PRIMARY KEY(workspace_id,id), UNIQUE(workspace_id,dedupe_key),
 FOREIGN KEY(workspace_id,run_id) REFERENCES app.runs(workspace_id,id) ON DELETE CASCADE,
 FOREIGN KEY(workspace_id,revision_id) REFERENCES app.document_revisions(workspace_id,id) ON DELETE CASCADE,
 CHECK((kind='chat' AND run_id IS NOT NULL) OR kind<>'chat'),
 CHECK((kind='ingest' AND revision_id IS NOT NULL) OR kind<>'ingest')
);
CREATE INDEX jobs_claim ON app.jobs(kind,status,available_at,created_at);
CREATE INDEX jobs_expired ON app.jobs(lease_until) WHERE status='running';
CREATE TABLE app.tool_executions (
 workspace_id uuid NOT NULL, id uuid NOT NULL, run_id uuid NOT NULL, user_id uuid NOT NULL,
 tool_name text NOT NULL, tool_version text NOT NULL, arguments jsonb NOT NULL, arguments_sha256 text NOT NULL,
 payload_sha256 text NOT NULL, status text NOT NULL CHECK(status IN ('proposed','awaiting_approval','approved','running','succeeded','failed','rejected','expired')),
 approval_expires_at timestamptz, approved_at timestamptz, approved_by uuid REFERENCES app.users,
 result jsonb, error_code text, created_at timestamptz NOT NULL DEFAULT now(),
 PRIMARY KEY(workspace_id,id), UNIQUE(workspace_id,run_id,tool_name,arguments_sha256), UNIQUE(workspace_id,id,user_id),
 FOREIGN KEY(workspace_id,run_id,user_id) REFERENCES app.runs(workspace_id,id,user_id) ON DELETE CASCADE
);
CREATE TABLE app.briefs (
 workspace_id uuid NOT NULL, id uuid NOT NULL, user_id uuid NOT NULL, execution_id uuid NOT NULL,
 answer_message_id uuid NOT NULL, title text NOT NULL CHECK(length(title)<=120),
 answer jsonb NOT NULL, citations jsonb NOT NULL, created_at timestamptz NOT NULL DEFAULT now(),
 PRIMARY KEY(workspace_id,id), UNIQUE(workspace_id,execution_id),
 FOREIGN KEY(workspace_id,execution_id,user_id) REFERENCES app.tool_executions(workspace_id,id,user_id) ON DELETE CASCADE,
 FOREIGN KEY(workspace_id,answer_message_id,user_id) REFERENCES app.messages(workspace_id,id,user_id) ON DELETE CASCADE
);
CREATE TABLE app.budget_windows (
 workspace_id uuid NOT NULL REFERENCES app.workspaces ON DELETE CASCADE,
 period text NOT NULL CHECK(period IN ('day','month')), starts_at date NOT NULL,
 limit_micro_usd bigint NOT NULL CHECK(limit_micro_usd>=0), reserved_micro_usd bigint NOT NULL DEFAULT 0 CHECK(reserved_micro_usd>=0),
 spent_micro_usd bigint NOT NULL DEFAULT 0 CHECK(spent_micro_usd>=0),
 PRIMARY KEY(workspace_id,period,starts_at)
);
CREATE TABLE app.model_usage (
 workspace_id uuid NOT NULL REFERENCES app.workspaces ON DELETE CASCADE, id uuid NOT NULL,
 run_id uuid, user_id uuid NOT NULL REFERENCES app.users, conversation_id uuid,
 attempt_no int NOT NULL CHECK(attempt_no>0), purpose text NOT NULL CHECK(purpose IN ('generation','rewrite','summary','evaluation')),
 model_key text NOT NULL, resolved_model_id text NOT NULL, price_version text NOT NULL,
 price_snapshot jsonb, inference_seconds double precision CHECK(inference_seconds>=0),
 input_tokens bigint CHECK(input_tokens>=0), output_tokens bigint CHECK(output_tokens>=0),
 cached_input_tokens bigint CHECK(cached_input_tokens>=0), reasoning_output_tokens bigint CHECK(reasoning_output_tokens>=0),
 reserved_micro_usd bigint NOT NULL CHECK(reserved_micro_usd>=0), cost_micro_usd bigint CHECK(cost_micro_usd>=0),
 request_sha256 text CHECK(request_sha256 ~ '^[a-f0-9]{64}$'),
 failure_code text CHECK(length(failure_code)<=80), failure_retryable boolean NOT NULL DEFAULT false,
 retry_after_seconds double precision CHECK(retry_after_seconds>=0 AND retry_after_seconds<'Infinity'::float8),
 health_recorded boolean NOT NULL DEFAULT false,
 execution_state text NOT NULL DEFAULT 'not_issued' CHECK(execution_state IN ('not_issued','issued','response_stored','definite_failure','ambiguous')),
 response jsonb, issued_at timestamptz,
 usage_estimated boolean NOT NULL DEFAULT false, state text NOT NULL CHECK(state IN ('reserved','settled','estimated')),
 day_start date NOT NULL, month_start date NOT NULL, created_at timestamptz NOT NULL DEFAULT now(),
 PRIMARY KEY(workspace_id,id), UNIQUE(workspace_id,run_id,attempt_no),
 FOREIGN KEY(workspace_id,run_id) REFERENCES app.runs(workspace_id,id),
 FOREIGN KEY(workspace_id,conversation_id) REFERENCES app.conversations(workspace_id,id)
);
CREATE INDEX usage_window ON app.model_usage(workspace_id,day_start,model_key);
CREATE INDEX usage_response_retention ON app.model_usage(workspace_id,created_at,id)
 WHERE response IS NOT NULL AND state IN ('settled','estimated');
CREATE TABLE app.feedback (
 workspace_id uuid NOT NULL, message_id uuid NOT NULL, user_id uuid NOT NULL,
 vote smallint NOT NULL CHECK(vote IN (-1,1)), reason text NOT NULL DEFAULT '' CHECK(length(reason)<=1000),
 updated_at timestamptz NOT NULL DEFAULT now(), PRIMARY KEY(workspace_id,message_id,user_id),
 FOREIGN KEY(workspace_id,message_id,user_id) REFERENCES app.messages(workspace_id,id,user_id) ON DELETE CASCADE
);
CREATE TABLE app.evaluation_runs (
 workspace_id uuid NOT NULL REFERENCES app.workspaces ON DELETE CASCADE, id uuid NOT NULL,
 created_by uuid NOT NULL REFERENCES app.users, dataset_version text NOT NULL, dataset_sha256 text NOT NULL,
 config_sha256 text NOT NULL, git_sha text, variant text NOT NULL, model_key text NOT NULL,
 source_snapshot_sha256 text CONSTRAINT evaluation_snapshot_hash CHECK(source_snapshot_sha256 ~ '^[a-f0-9]{64}$'),
 evidence_scope text NOT NULL DEFAULT 'legacy_unclassified'
   CHECK(evidence_scope IN ('draft_diagnostic','reviewed_benchmark','operational','legacy_unclassified')),
 CONSTRAINT evaluation_provenance_required CHECK(evidence_scope='legacy_unclassified' OR source_snapshot_sha256 IS NOT NULL),
 hardware text NOT NULL, status text NOT NULL CHECK(status IN ('completed','failed')),
 case_count int NOT NULL CHECK(case_count>0), metrics jsonb NOT NULL, artifact_sha256 text NOT NULL,
 created_at timestamptz NOT NULL DEFAULT now(), PRIMARY KEY(workspace_id,id)
);
CREATE TABLE app.evaluation_cases (
 workspace_id uuid NOT NULL, evaluation_id uuid NOT NULL, case_id text NOT NULL,
 status text NOT NULL CHECK(status IN ('passed','failed','error')), metrics jsonb NOT NULL,
 failure_code text, PRIMARY KEY(workspace_id,evaluation_id,case_id),
 FOREIGN KEY(workspace_id,evaluation_id) REFERENCES app.evaluation_runs(workspace_id,id) ON DELETE CASCADE
);
CREATE TABLE app.audit_events (
 workspace_id uuid NOT NULL REFERENCES app.workspaces ON DELETE CASCADE, id uuid NOT NULL,
 actor_id uuid REFERENCES app.users, action text NOT NULL, target_type text NOT NULL, target_id uuid,
 request_id uuid NOT NULL, metadata jsonb NOT NULL DEFAULT '{}', created_at timestamptz NOT NULL DEFAULT now(),
 PRIMARY KEY(workspace_id,id)
);
CREATE INDEX audit_time ON app.audit_events(workspace_id,created_at,id);
CREATE TABLE app.idempotency_keys (
 workspace_id uuid NOT NULL REFERENCES app.workspaces ON DELETE CASCADE, user_id uuid NOT NULL REFERENCES app.users,
 method_path text NOT NULL, key_hash text NOT NULL, request_sha256 text NOT NULL,
 response_status int, response_body jsonb, expires_at timestamptz NOT NULL,
 PRIMARY KEY(workspace_id,user_id,method_path,key_hash)
);
CREATE INDEX idempotency_expiry ON app.idempotency_keys(workspace_id,expires_at);
CREATE TABLE app.deletion_tombstones (
 workspace_id uuid NOT NULL, id uuid NOT NULL, entity_type text NOT NULL,
 entity_id uuid NOT NULL, deleted_at timestamptz NOT NULL DEFAULT now(), purged_at timestamptz,
 object_manifest jsonb NOT NULL DEFAULT '[]', PRIMARY KEY(workspace_id,id), UNIQUE(workspace_id,entity_type,entity_id)
 -- Deliberately no workspace FK: survive workspace physical deletion for restore fencing.
);
CREATE INDEX checkpoint_tombstone_due ON app.deletion_tombstones(workspace_id,purged_at,entity_id)
 WHERE entity_type='workflow_thread';
CREATE TABLE app.provider_health (
 model_key text PRIMARY KEY, state text NOT NULL CHECK(state IN ('closed','open','half_open','disabled')),
 failure_times timestamptz[] NOT NULL DEFAULT '{}' CHECK(cardinality(failure_times)<=5),
 probe_token uuid, probe_until timestamptz,
 probe_budget_day date, probe_reserved_micro_usd bigint NOT NULL DEFAULT 0 CHECK(probe_reserved_micro_usd>=0 AND probe_reserved_micro_usd<=1000000),
 transient_failures int NOT NULL DEFAULT 0, window_start timestamptz NOT NULL,
 open_until timestamptz, last_probe_at timestamptz, capability_report jsonb NOT NULL DEFAULT '{}'
);
CREATE TABLE app.rate_windows (
 user_id uuid NOT NULL REFERENCES app.users, category text NOT NULL, window_start timestamptz NOT NULL,
 count int NOT NULL CHECK(count>=0), PRIMARY KEY(user_id,category,window_start)
);
-- Business mapping only. Vendor saver setup owns its internal tables in lg_checkpoints schema.
CREATE TABLE app.workflow_threads (
 workspace_id uuid NOT NULL, id uuid NOT NULL, user_id uuid NOT NULL, run_id uuid NOT NULL,
 tool_execution_id uuid, graph_name text NOT NULL CHECK(graph_name IN ('answer_v1','brief_v1')),
 graph_version text NOT NULL, state_schema_version int NOT NULL DEFAULT 1 CHECK(state_schema_version>0),
 status text NOT NULL CHECK(status IN ('running','waiting','completed','failed','cancelled','expired')),
 expires_at timestamptz NOT NULL, created_at timestamptz NOT NULL DEFAULT now(),
 checkpoint_purged_at timestamptz,
 PRIMARY KEY(workspace_id,id), UNIQUE(id),
 FOREIGN KEY(workspace_id,run_id,user_id) REFERENCES app.runs(workspace_id,id,user_id) ON DELETE CASCADE,
 FOREIGN KEY(workspace_id,tool_execution_id,user_id) REFERENCES app.tool_executions(workspace_id,id,user_id) ON DELETE CASCADE,
 CHECK((graph_name='brief_v1')=(tool_execution_id IS NOT NULL))
);
CREATE UNIQUE INDEX workflow_answer_once ON app.workflow_threads(workspace_id,run_id) WHERE graph_name='answer_v1';
CREATE UNIQUE INDEX workflow_brief_once ON app.workflow_threads(workspace_id,tool_execution_id) WHERE graph_name='brief_v1';
CREATE INDEX workflow_retention_due ON app.workflow_threads(workspace_id,expires_at,id);
-- Fail-closed scope helpers. API verifies membership before setting these locally.
CREATE FUNCTION app.workspace_scope() RETURNS uuid LANGUAGE sql STABLE AS
 $$ SELECT nullif(current_setting('app.workspace_id',true),'')::uuid $$;
CREATE FUNCTION app.user_scope() RETURNS uuid LANGUAGE sql STABLE AS
 $$ SELECT nullif(current_setting('app.user_id',true),'')::uuid $$;
-- Generic RLS constrains tenant only; domain services enforce roles/creator/tombstones.
DO $$ DECLARE t text; BEGIN
 FOREACH t IN ARRAY ARRAY['memberships','knowledge_bases','documents','document_revisions','chunks','index_manifests','index_outbox','index_orphans','workflow_threads',
 'conversations','messages','runs','run_events','jobs','tool_executions','briefs','budget_windows','model_usage','feedback',
 'evaluation_runs','evaluation_cases','audit_events','idempotency_keys','deletion_tombstones'] LOOP
  EXECUTE format('ALTER TABLE app.%I ENABLE ROW LEVEL SECURITY',t);
  EXECUTE format('ALTER TABLE app.%I FORCE ROW LEVEL SECURITY',t);
  EXECUTE format('CREATE POLICY tenant_scope ON app.%I USING (workspace_id=app.workspace_scope()) WITH CHECK (workspace_id=app.workspace_scope())',t);
 END LOOP;
END $$;
ALTER TABLE app.workspaces ENABLE ROW LEVEL SECURITY;
ALTER TABLE app.workspaces FORCE ROW LEVEL SECURITY;
CREATE POLICY workspace_scope ON app.workspaces USING(id=app.workspace_scope()) WITH CHECK(id=app.workspace_scope());
-- Global authentication events cannot truthfully be assigned to a workspace.
CREATE TABLE app.identity_audit_events (
 id uuid PRIMARY KEY DEFAULT gen_random_uuid(), user_id uuid REFERENCES app.users,
 action text NOT NULL CHECK(action IN ('login','logout')), request_id uuid NOT NULL,
 occurred_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
COMMIT;
