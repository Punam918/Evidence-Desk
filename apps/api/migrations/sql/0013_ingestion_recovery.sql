CREATE OR REPLACE FUNCTION app.sweep_expired_jobs() RETURNS int
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
     charge=CASE WHEN usage_row.execution_state IN ('not_issued','definite_failure') THEN 0 ELSE usage_row.reserved_micro_usd END;
     UPDATE app.budget_windows SET reserved_micro_usd=greatest(0,reserved_micro_usd-usage_row.reserved_micro_usd),spent_micro_usd=spent_micro_usd+charge
       WHERE workspace_id=j.workspace_id AND ((period='day' AND starts_at=usage_row.day_start) OR (period='month' AND starts_at=usage_row.month_start));
     UPDATE app.model_usage SET state=CASE WHEN usage_row.execution_state IN ('not_issued','definite_failure') THEN 'settled' ELSE 'estimated' END,cost_micro_usd=charge,usage_estimated=(usage_row.execution_state NOT IN ('not_issued','definite_failure')),
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
   IF j.kind='ingest' THEN
    UPDATE app.document_revisions SET status=CASE WHEN j.attempts<3 THEN 'retry_wait' ELSE 'failed' END
      WHERE workspace_id=j.workspace_id AND id=j.revision_id AND status NOT IN ('published','superseded','deleting','deleted','cancelled');
   END IF;
  END IF;
 END LOOP;
 UPDATE app.admission_counters SET active_runs=(SELECT count(*) FROM app.runs WHERE status IN ('queued','running')),updated_at=clock_timestamp() WHERE singleton;
 UPDATE app.index_outbox SET state='dead',error_code='attempts_exhausted' WHERE attempts>=3 AND state='leased' AND lease_until<=clock_timestamp();
 RETURN n;
END $$;

GRANT SELECT,UPDATE ON app.document_revisions TO ed_dispatch_owner;
