CREATE TABLE app.identity_audit_events (
 id uuid PRIMARY KEY DEFAULT gen_random_uuid(), user_id uuid REFERENCES app.users,
 action text NOT NULL CHECK(action IN ('login','logout')), request_id uuid NOT NULL,
 occurred_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
REVOKE ALL ON app.identity_audit_events FROM PUBLIC;
GRANT INSERT ON app.identity_audit_events TO ed_identity_owner;
CREATE FUNCTION app.record_identity_audit(p_user uuid,p_action text,p_request uuid) RETURNS void
LANGUAGE sql SECURITY DEFINER SET search_path=pg_catalog,app AS $$
 INSERT INTO app.identity_audit_events(user_id,action,request_id) VALUES(p_user,p_action,p_request);
$$;
ALTER FUNCTION app.record_identity_audit(uuid,text,uuid) OWNER TO ed_identity_owner;
REVOKE ALL ON FUNCTION app.record_identity_audit(uuid,text,uuid) FROM PUBLIC;
-- Called within finish/logout transactions only; not directly exposed to runtime identity.
