CREATE FUNCTION app.consume_rate(p_actor uuid,p_category text,p_limit int) RETURNS boolean
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,app AS $$
DECLARE used int;
BEGIN
 INSERT INTO app.rate_windows(user_id,category,window_start,count) VALUES(p_actor,p_category,date_trunc('minute',clock_timestamp()),1)
 ON CONFLICT(user_id,category,window_start) DO UPDATE SET count=app.rate_windows.count+1 RETURNING count INTO used;
 RETURN used<=p_limit;
END $$;
CREATE FUNCTION app.logout_session(p_hash text,p_key text,p_body text,p_request uuid DEFAULT gen_random_uuid()) RETURNS void
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,app AS $$
DECLARE actor uuid; changed int;
BEGIN
 SELECT user_id INTO actor FROM app.sessions WHERE id_hash=p_hash FOR UPDATE;
 IF actor IS NULL THEN RETURN; END IF;
 INSERT INTO app.identity_idempotency_keys(user_id,method_path,key_hash,request_sha256,response_status,response_body,expires_at)
 VALUES(actor,'POST /auth/logout',p_key,p_body,204,'{}',clock_timestamp()+interval '24 hours')
 ON CONFLICT(user_id,method_path,key_hash) DO NOTHING;
 UPDATE app.sessions SET revoked_at=clock_timestamp() WHERE id_hash=p_hash AND revoked_at IS NULL;
 GET DIAGNOSTICS changed=ROW_COUNT;
 IF changed>0 THEN PERFORM app.record_identity_audit(actor,'logout',p_request); END IF;
END $$;
CREATE FUNCTION app.reserve_run_slot() RETURNS boolean
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,app AS $$
DECLARE total int; workspace_active int;
BEGIN
 PERFORM 1 FROM app.admission_counters WHERE singleton FOR UPDATE;
 IF NOT EXISTS(SELECT 1 FROM app.memberships m JOIN app.workspaces w ON w.id=m.workspace_id
   WHERE m.workspace_id=app.workspace_scope() AND m.user_id=app.user_scope() AND w.deleted_at IS NULL)
 THEN RETURN false; END IF;
 SELECT count(*) INTO total FROM app.runs WHERE status IN ('queued','running');
 SELECT count(*) INTO workspace_active FROM app.runs WHERE workspace_id=app.workspace_scope() AND status IN ('queued','running');
 UPDATE app.admission_counters SET active_runs=total,updated_at=clock_timestamp() WHERE singleton;
 IF total>=5 OR workspace_active>=2 THEN RETURN false; END IF;
 UPDATE app.admission_counters SET active_runs=total+1 WHERE singleton;
 RETURN true;
END $$;
ALTER FUNCTION app.consume_rate(uuid,text,int) OWNER TO ed_identity_owner;
ALTER FUNCTION app.logout_session(text,text,text,uuid) OWNER TO ed_identity_owner;
ALTER FUNCTION app.reserve_run_slot() OWNER TO ed_dispatch_owner;
REVOKE ALL ON FUNCTION app.consume_rate(uuid,text,int),app.logout_session(text,text,text,uuid),app.reserve_run_slot() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION app.consume_rate(uuid,text,int),app.logout_session(text,text,text,uuid) TO ed_identity;
GRANT EXECUTE ON FUNCTION app.reserve_run_slot() TO ed_app;
GRANT EXECUTE ON FUNCTION app.workspace_scope(),app.user_scope() TO ed_dispatch_owner;
GRANT SELECT ON app.memberships TO ed_dispatch_owner;
