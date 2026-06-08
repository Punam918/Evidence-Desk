CREATE INDEX checkpoint_tombstone_due ON app.deletion_tombstones(workspace_id,purged_at,entity_id)
 WHERE entity_type='workflow_thread';
CREATE INDEX conversation_runs ON app.runs(workspace_id,conversation_id,id);
CREATE FUNCTION app.claim_conversation_purge(p_worker text)
RETURNS TABLE(workspace_id uuid,job_id uuid,actor_id uuid,run_id uuid,revision_id uuid,lease_token bigint,kind text)
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,app AS $$
DECLARE chosen app.jobs;
BEGIN
 PERFORM pg_advisory_xact_lock(hashtextextended('ed-job-claim:purge',0));
 SELECT j.* INTO chosen FROM app.jobs j JOIN app.workspaces w ON w.id=j.workspace_id
 WHERE j.kind='purge' AND j.payload->>'entity_type'='conversation'
 AND j.status IN ('queued','retry_wait') AND j.attempts<3 AND j.available_at<=clock_timestamp()
 ORDER BY (SELECT max(recent.updated_at) FROM app.jobs recent
   WHERE recent.workspace_id=j.workspace_id AND recent.kind='purge' AND recent.attempts>0) NULLS FIRST,
   j.created_at,j.id
 FOR UPDATE OF j SKIP LOCKED LIMIT 1;
 IF NOT FOUND THEN RETURN; END IF;
 RETURN QUERY UPDATE app.jobs j SET status='running',attempts=j.attempts+1,lease_token=j.lease_token+1,
   lease_until=clock_timestamp()+interval '30 seconds',worker_id=p_worker,updated_at=clock_timestamp()
 WHERE j.workspace_id=chosen.workspace_id AND j.id=chosen.id
 RETURNING j.workspace_id,j.id,j.requested_by,j.run_id,j.revision_id,j.lease_token,j.kind;
END $$;
ALTER FUNCTION app.claim_conversation_purge(text) OWNER TO ed_dispatch_owner;
REVOKE ALL ON FUNCTION app.claim_conversation_purge(text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION app.claim_conversation_purge(text) TO ed_dispatch;
