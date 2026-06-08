CREATE FUNCTION app.start_login(p_state text,p_nonce text,p_cipher bytea,p_return text) RETURNS void
LANGUAGE sql SECURITY DEFINER SET search_path=pg_catalog,app AS $$
 INSERT INTO app.login_states(state_hash,nonce_hash,pkce_verifier_ciphertext,return_path,expires_at)
 VALUES(p_state,p_nonce,p_cipher,p_return,clock_timestamp()+interval '5 minutes');
$$;
CREATE FUNCTION app.consume_login(p_state text) RETURNS TABLE(nonce_hash text,cipher bytea,return_path text)
LANGUAGE sql SECURITY DEFINER SET search_path=pg_catalog,app AS $$
 UPDATE app.login_states SET consumed_at=clock_timestamp()
 WHERE state_hash=p_state AND consumed_at IS NULL AND expires_at>clock_timestamp()
 RETURNING nonce_hash,pkce_verifier_ciphertext,return_path;
$$;
CREATE FUNCTION app.finish_login(p_issuer text,p_subject text,p_email text,p_name text,p_hash text,p_csrf text,p_request uuid DEFAULT gen_random_uuid())
RETURNS uuid LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,app AS $$
DECLARE u uuid;
BEGIN
 INSERT INTO app.users(id,issuer,subject,email,display_name)
 VALUES(gen_random_uuid(),p_issuer,p_subject,lower(p_email),left(p_name,120))
 ON CONFLICT(issuer,subject) DO UPDATE SET email=excluded.email,display_name=excluded.display_name
 RETURNING id INTO u;
 INSERT INTO app.sessions(id_hash,user_id,csrf_hash,expires_at)
 VALUES(p_hash,u,p_csrf,clock_timestamp()+interval '24 hours');
 PERFORM app.record_identity_audit(u,'login',p_request);
 RETURN u;
END $$;
CREATE FUNCTION app.lookup_session(p_hash text)
RETURNS TABLE(user_id uuid,email text,display_name text,expires_at timestamptz,csrf_hash text)
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,app AS $$
BEGIN
 UPDATE app.sessions s SET last_seen_at=clock_timestamp()
 WHERE s.id_hash=p_hash AND s.revoked_at IS NULL AND s.expires_at>clock_timestamp()
 AND s.last_seen_at>clock_timestamp()-interval '8 hours'
 AND s.last_seen_at<clock_timestamp()-interval '5 minutes';
 RETURN QUERY SELECT u.id,u.email,u.display_name,s.expires_at,s.csrf_hash
 FROM app.sessions s JOIN app.users u ON u.id=s.user_id
 WHERE s.id_hash=p_hash AND s.revoked_at IS NULL AND s.expires_at>clock_timestamp()
 AND s.last_seen_at>clock_timestamp()-interval '8 hours';
END $$;
CREATE FUNCTION app.revoke_session(p_hash text) RETURNS void
LANGUAGE sql SECURITY DEFINER SET search_path=pg_catalog,app AS $$
 UPDATE app.sessions SET revoked_at=clock_timestamp() WHERE id_hash=p_hash AND revoked_at IS NULL;
$$;
CREATE FUNCTION app.get_membership(p_actor uuid,p_workspace uuid)
RETURNS TABLE(role text,version bigint) LANGUAGE sql SECURITY DEFINER SET search_path=pg_catalog,app AS $$
 SELECT m.role,w.version FROM app.memberships m JOIN app.workspaces w ON w.id=m.workspace_id
 WHERE m.user_id=p_actor AND w.id=p_workspace AND w.deleted_at IS NULL;
$$;
-- Narrow function owners have no credentials. Runtime can execute only these signatures.
DO $$ DECLARE f regprocedure; BEGIN
 FOR f IN SELECT p.oid::regprocedure FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace
 WHERE n.nspname='app' AND p.proname IN ('start_login','consume_login','finish_login','lookup_session','revoke_session','get_membership') LOOP
  EXECUTE format('ALTER FUNCTION %s OWNER TO ed_identity_owner',f);
  EXECUTE format('REVOKE ALL ON FUNCTION %s FROM PUBLIC',f);
  EXECUTE format('GRANT EXECUTE ON FUNCTION %s TO ed_identity',f);
 END LOOP;
END $$;
