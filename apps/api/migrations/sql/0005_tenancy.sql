CREATE FUNCTION app.create_workspace(p_actor uuid,p_id uuid,p_name text,p_key text,p_body text)
RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,app AS $$
DECLARE cached app.identity_idempotency_keys; v_workspace app.workspaces; result jsonb;
BEGIN
 PERFORM 1 FROM app.users WHERE id=p_actor FOR UPDATE;
 IF NOT FOUND THEN RAISE EXCEPTION 'unknown actor' USING ERRCODE='P0002'; END IF;
 SELECT * INTO cached FROM app.identity_idempotency_keys WHERE user_id=p_actor AND method_path='POST /v1/workspaces' AND key_hash=p_key;
 IF FOUND AND cached.expires_at>clock_timestamp() THEN
  IF cached.request_sha256<>p_body THEN RAISE EXCEPTION 'idempotency_conflict' USING ERRCODE='P0001'; END IF;
  IF NOT EXISTS(SELECT 1 FROM app.memberships m JOIN app.workspaces w ON w.id=m.workspace_id
    WHERE m.user_id=p_actor AND w.id=(cached.response_body->>'id')::uuid AND w.deleted_at IS NULL)
    THEN RAISE EXCEPTION 'workspace unavailable' USING ERRCODE='P0002'; END IF;
  RETURN cached.response_body;
 END IF;
 IF (SELECT count(*) FROM app.memberships m JOIN app.workspaces w ON w.id=m.workspace_id
     WHERE m.user_id=p_actor AND m.role='owner' AND w.deleted_at IS NULL)>=5
 THEN RAISE EXCEPTION 'workspace_quota' USING ERRCODE='P0001'; END IF;
 INSERT INTO app.workspaces(id,name) VALUES(p_id,p_name) RETURNING * INTO v_workspace;
 INSERT INTO app.memberships(workspace_id,user_id,role) VALUES(p_id,p_actor,'owner');
 INSERT INTO app.budget_windows(workspace_id,period,starts_at,limit_micro_usd) VALUES
 (p_id,'day',(clock_timestamp() AT TIME ZONE 'UTC')::date,v_workspace.daily_budget_micro_usd),
 (p_id,'month',date_trunc('month',clock_timestamp() AT TIME ZONE 'UTC')::date,v_workspace.monthly_budget_micro_usd);
 result=jsonb_build_object('id',v_workspace.id,'name',v_workspace.name,'role','owner','created_at',v_workspace.created_at,'version',v_workspace.version);
 INSERT INTO app.identity_idempotency_keys(user_id,method_path,key_hash,request_sha256,response_status,response_body,expires_at)
 VALUES(p_actor,'POST /v1/workspaces',p_key,p_body,201,result,clock_timestamp()+interval '24 hours')
 ON CONFLICT(user_id,method_path,key_hash) DO UPDATE SET request_sha256=excluded.request_sha256,response_status=201,response_body=excluded.response_body,expires_at=excluded.expires_at;
 RETURN result;
END $$;
CREATE FUNCTION app.list_user_workspaces(p_actor uuid,p_after timestamptz,p_id uuid,p_limit int)
RETURNS TABLE(id uuid,name text,role text,created_at timestamptz,version bigint)
LANGUAGE sql SECURITY DEFINER SET search_path=pg_catalog,app AS $$
 SELECT w.id,w.name,m.role,w.created_at,w.version FROM app.workspaces w JOIN app.memberships m ON m.workspace_id=w.id
 WHERE m.user_id=p_actor AND w.deleted_at IS NULL AND (p_after IS NULL OR (w.created_at,w.id)>(p_after,p_id))
 ORDER BY w.created_at,w.id LIMIT least(greatest(p_limit,1),101);
$$;
CREATE FUNCTION app.member_email(p_target uuid) RETURNS text
LANGUAGE sql SECURITY DEFINER SET search_path=pg_catalog,app AS $$
 SELECT u.email FROM app.users u JOIN app.memberships target ON target.user_id=u.id
 JOIN app.workspaces w ON w.id=target.workspace_id
 WHERE u.id=p_target AND target.workspace_id=app.workspace_scope() AND w.deleted_at IS NULL
 AND EXISTS(SELECT 1 FROM app.memberships actor WHERE actor.workspace_id=w.id AND actor.user_id=app.user_scope());
$$;
CREATE FUNCTION app.find_member_user(p_email text) RETURNS uuid
LANGUAGE sql SECURITY DEFINER SET search_path=pg_catalog,app AS $$
 SELECT u.id FROM app.users u WHERE u.email=lower(p_email)
 AND EXISTS(SELECT 1 FROM app.memberships m JOIN app.workspaces w ON w.id=m.workspace_id
 WHERE m.workspace_id=app.workspace_scope() AND m.user_id=app.user_scope() AND m.role='owner' AND w.deleted_at IS NULL);
$$;
GRANT EXECUTE ON FUNCTION app.workspace_scope(),app.user_scope() TO ed_identity_owner;
DO $$ DECLARE f regprocedure; BEGIN
 FOR f IN SELECT p.oid::regprocedure FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace
 WHERE n.nspname='app' AND p.proname IN ('create_workspace','list_user_workspaces','member_email','find_member_user') LOOP
  EXECUTE format('ALTER FUNCTION %s OWNER TO ed_identity_owner',f);
  EXECUTE format('REVOKE ALL ON FUNCTION %s FROM PUBLIC',f);
 END LOOP;
END $$;
GRANT EXECUTE ON FUNCTION app.create_workspace(uuid,uuid,text,text,text),app.list_user_workspaces(uuid,timestamptz,uuid,int) TO ed_identity;
GRANT EXECUTE ON FUNCTION app.member_email(uuid),app.find_member_user(text) TO ed_app;
