CREATE FUNCTION app.claim_job(p_kind text,p_worker text)
RETURNS TABLE(workspace_id uuid,job_id uuid,actor_id uuid,run_id uuid,revision_id uuid,lease_token bigint,kind text)
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,app AS $$
DECLARE chosen app.jobs;
BEGIN
 -- Short per-kind claim serialization gives round-robin workspace ordering a stable view.
 PERFORM pg_advisory_xact_lock(hashtextextended('ed-job-claim:'||p_kind,0));
 SELECT j.* INTO chosen FROM app.jobs j JOIN app.workspaces w ON w.id=j.workspace_id
 WHERE j.kind=p_kind AND j.status IN ('queued','retry_wait') AND j.attempts<3
 AND j.available_at<=clock_timestamp() AND (w.deleted_at IS NULL OR j.kind='purge')
 ORDER BY (SELECT max(recent.updated_at) FROM app.jobs recent WHERE recent.workspace_id=j.workspace_id AND recent.kind=p_kind AND recent.attempts>0) NULLS FIRST,j.created_at,j.id
 FOR UPDATE OF j SKIP LOCKED LIMIT 1;
 IF NOT FOUND THEN RETURN; END IF;
 RETURN QUERY UPDATE app.jobs j SET status='running',attempts=j.attempts+1,lease_token=j.lease_token+1,
  lease_until=clock_timestamp()+interval '30 seconds',worker_id=p_worker,updated_at=clock_timestamp()
  WHERE j.workspace_id=chosen.workspace_id AND j.id=chosen.id
  RETURNING j.workspace_id,j.id,j.requested_by,j.run_id,j.revision_id,j.lease_token,j.kind;
END $$;
CREATE FUNCTION app.renew_job(p_workspace uuid,p_job uuid,p_worker text,p_token bigint) RETURNS boolean
LANGUAGE sql SECURITY DEFINER SET search_path=pg_catalog,app AS $$
 WITH renewed AS (UPDATE app.jobs SET lease_until=clock_timestamp()+interval '30 seconds',updated_at=clock_timestamp()
 WHERE workspace_id=p_workspace AND id=p_job AND worker_id=p_worker AND lease_token=p_token AND status='running' AND lease_until>clock_timestamp() RETURNING 1)
 SELECT EXISTS(SELECT 1 FROM renewed);
$$;
CREATE FUNCTION app.claim_index_outbox()
RETURNS TABLE(workspace_id uuid,outbox_id uuid,revision_id uuid,profile_id text,operation text,lease_token bigint)
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,app AS $$
DECLARE chosen app.index_outbox;
BEGIN
 SELECT * INTO chosen FROM app.index_outbox o WHERE o.attempts<3 AND o.next_attempt_at<=clock_timestamp()
 AND (o.state='pending' OR (o.state='leased' AND o.lease_until<clock_timestamp()))
 ORDER BY o.created_at,o.id FOR UPDATE SKIP LOCKED LIMIT 1;
 IF NOT FOUND THEN RETURN; END IF;
 RETURN QUERY UPDATE app.index_outbox o SET state='leased',attempts=o.attempts+1,
 lease_token=o.lease_token+1,lease_until=clock_timestamp()+interval '30 seconds'
 WHERE o.workspace_id=chosen.workspace_id AND o.id=chosen.id
 RETURNING o.workspace_id,o.id,o.revision_id,o.profile_id,o.operation,o.lease_token;
END $$;
CREATE FUNCTION app.sweep_expired_jobs() RETURNS int
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,app AS $$
DECLARE j app.jobs; r app.runs; usage_row app.model_usage; n int=0; charge bigint; unsafe boolean;
BEGIN
 PERFORM 1 FROM app.admission_counters WHERE singleton FOR UPDATE;
 FOR j IN SELECT * FROM app.jobs WHERE status='running' AND lease_until<=clock_timestamp() ORDER BY created_at FOR UPDATE SKIP LOCKED LOOP
  n=n+1;
  IF j.kind='chat' THEN
   SELECT * INTO r FROM app.runs WHERE workspace_id=j.workspace_id AND id=j.run_id FOR UPDATE;
   IF r.status NOT IN ('queued','running') THEN
    UPDATE app.jobs SET status='cancelled',lease_until=NULL,updated_at=clock_timestamp() WHERE workspace_id=j.workspace_id AND id=j.id;
    CONTINUE;
   END IF;
   unsafe=EXISTS(SELECT 1 FROM app.model_usage WHERE workspace_id=j.workspace_id AND run_id=j.run_id AND execution_state IN ('issued','ambiguous'))
       OR (EXISTS(SELECT 1 FROM app.run_events WHERE workspace_id=j.workspace_id AND run_id=j.run_id AND type='answer.delta')
         AND NOT EXISTS(SELECT 1 FROM app.model_usage WHERE workspace_id=j.workspace_id AND run_id=j.run_id AND execution_state='response_stored'));
   IF unsafe OR j.attempts>=3 OR r.created_at<clock_timestamp()-interval '60 seconds' OR r.cancel_requested THEN
    FOR usage_row IN SELECT * FROM app.model_usage WHERE workspace_id=j.workspace_id AND run_id=j.run_id AND state='reserved' FOR UPDATE LOOP
     charge=CASE WHEN usage_row.execution_state='not_issued' THEN 0 ELSE usage_row.reserved_micro_usd END;
     UPDATE app.budget_windows SET reserved_micro_usd=greatest(0,reserved_micro_usd-usage_row.reserved_micro_usd),spent_micro_usd=spent_micro_usd+charge
       WHERE workspace_id=j.workspace_id AND ((period='day' AND starts_at=usage_row.day_start) OR (period='month' AND starts_at=usage_row.month_start));
     UPDATE app.model_usage SET state=CASE WHEN charge=0 THEN 'settled' ELSE 'estimated' END,cost_micro_usd=charge,usage_estimated=(charge>0),
       execution_state=CASE WHEN execution_state='issued' THEN 'ambiguous' ELSE execution_state END
       WHERE workspace_id=j.workspace_id AND id=usage_row.id;
    END LOOP;
    UPDATE app.runs SET status='interrupted',error_code='worker_interrupted',completed_at=clock_timestamp(),next_event_seq=next_event_seq+1
      WHERE workspace_id=j.workspace_id AND id=j.run_id;
    UPDATE app.messages SET state='interrupted',text='',answer=NULL,citations='[]' WHERE workspace_id=j.workspace_id AND id=r.assistant_message_id;
    INSERT INTO app.run_events(workspace_id,run_id,sequence,type,data)
      VALUES(j.workspace_id,j.run_id,r.next_event_seq,'run.failed','{"code":"worker_interrupted","retryable":false,"discard_draft":true}');
    UPDATE app.jobs SET status='dead',lease_until=NULL,error_code='worker_interrupted',updated_at=clock_timestamp() WHERE workspace_id=j.workspace_id AND id=j.id;
   ELSE
    UPDATE app.jobs SET status='retry_wait',lease_until=NULL,available_at=clock_timestamp(),updated_at=clock_timestamp() WHERE workspace_id=j.workspace_id AND id=j.id;
   END IF;
  ELSE
   UPDATE app.jobs SET status=CASE WHEN attempts<3 THEN 'retry_wait' ELSE 'dead' END,
     lease_until=NULL,available_at=clock_timestamp(),updated_at=clock_timestamp(),error_code='lease_expired'
     WHERE workspace_id=j.workspace_id AND id=j.id;
  END IF;
 END LOOP;
 UPDATE app.admission_counters SET active_runs=(SELECT count(*) FROM app.runs WHERE status IN ('queued','running')),updated_at=clock_timestamp() WHERE singleton;
 UPDATE app.index_outbox SET state='dead',error_code='attempts_exhausted' WHERE attempts>=3 AND state='leased' AND lease_until<=clock_timestamp();
 RETURN n;
END $$;
GRANT SELECT,UPDATE ON app.budget_windows,app.messages TO ed_dispatch_owner;
DO $$ DECLARE f regprocedure; BEGIN
 FOR f IN SELECT p.oid::regprocedure FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace
 WHERE n.nspname='app' AND p.proname IN ('claim_job','renew_job','claim_index_outbox','sweep_expired_jobs') LOOP
  EXECUTE format('ALTER FUNCTION %s OWNER TO ed_dispatch_owner',f);
  EXECUTE format('REVOKE ALL ON FUNCTION %s FROM PUBLIC',f);
  EXECUTE format('GRANT EXECUTE ON FUNCTION %s TO ed_dispatch',f);
 END LOOP;
END $$;
GRANT SELECT ON app.run_events TO ed_dispatch_owner;
