DO $$ BEGIN
 IF NOT EXISTS(SELECT 1 FROM pg_roles WHERE rolname='ed_app') THEN CREATE ROLE ed_app LOGIN NOSUPERUSER NOBYPASSRLS; END IF;
 IF NOT EXISTS(SELECT 1 FROM pg_roles WHERE rolname='ed_identity') THEN CREATE ROLE ed_identity LOGIN NOSUPERUSER NOBYPASSRLS; END IF;
 IF NOT EXISTS(SELECT 1 FROM pg_roles WHERE rolname='ed_dispatch') THEN CREATE ROLE ed_dispatch LOGIN NOSUPERUSER NOBYPASSRLS; END IF;
 IF NOT EXISTS(SELECT 1 FROM pg_roles WHERE rolname='ed_checkpoint') THEN CREATE ROLE ed_checkpoint LOGIN NOSUPERUSER NOBYPASSRLS; END IF;
 IF NOT EXISTS(SELECT 1 FROM pg_roles WHERE rolname='ed_identity_owner') THEN CREATE ROLE ed_identity_owner NOLOGIN NOSUPERUSER BYPASSRLS; END IF;
 IF NOT EXISTS(SELECT 1 FROM pg_roles WHERE rolname='ed_dispatch_owner') THEN CREATE ROLE ed_dispatch_owner NOLOGIN NOSUPERUSER BYPASSRLS; END IF;
END $$;
REVOKE ALL ON SCHEMA app FROM PUBLIC;
REVOKE ALL ON ALL TABLES IN SCHEMA app FROM PUBLIC;
REVOKE EXECUTE ON ALL FUNCTIONS IN SCHEMA app FROM PUBLIC;
GRANT USAGE ON SCHEMA app TO ed_app,ed_identity,ed_dispatch,ed_identity_owner,ed_dispatch_owner;
GRANT EXECUTE ON FUNCTION app.workspace_scope(),app.user_scope() TO ed_app;
DO $$ DECLARE t text; BEGIN
 FOREACH t IN ARRAY ARRAY['workspaces','memberships','knowledge_bases','documents','document_revisions','chunks','index_manifests','index_outbox','workflow_threads','conversations','messages','runs','run_events','jobs','tool_executions','briefs','budget_windows','model_usage','feedback','evaluation_runs','evaluation_cases','audit_events','idempotency_keys','deletion_tombstones'] LOOP
  EXECUTE format('GRANT SELECT,INSERT,UPDATE,DELETE ON app.%I TO ed_app',t);
 END LOOP;
END $$;
GRANT SELECT ON app.embedding_profiles TO ed_app;
REVOKE UPDATE,DELETE ON app.audit_events FROM ed_app;
GRANT SELECT,INSERT,UPDATE,DELETE ON app.users,app.sessions,app.login_states,app.identity_idempotency_keys,app.workspaces,app.memberships,app.budget_windows,app.rate_windows TO ed_identity_owner;
GRANT SELECT,UPDATE ON app.jobs,app.runs,app.model_usage,app.admission_counters,app.index_outbox TO ed_dispatch_owner;
GRANT SELECT ON app.workspaces TO ed_dispatch_owner;
GRANT INSERT ON app.run_events TO ed_dispatch_owner;
CREATE SCHEMA IF NOT EXISTS lg_checkpoints;
REVOKE ALL ON SCHEMA lg_checkpoints FROM PUBLIC;
GRANT USAGE ON SCHEMA lg_checkpoints TO ed_checkpoint;
ALTER ROLE ed_checkpoint SET search_path TO lg_checkpoints;
ALTER ROLE ed_app SET statement_timeout TO '3s';
ALTER ROLE ed_app SET lock_timeout TO '1s';
