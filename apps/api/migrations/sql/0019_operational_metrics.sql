DO $$ BEGIN
 IF NOT EXISTS(SELECT 1 FROM pg_roles WHERE rolname='ed_monitor') THEN
   CREATE ROLE ed_monitor LOGIN NOSUPERUSER NOBYPASSRLS;
 END IF;
 IF NOT EXISTS(SELECT 1 FROM pg_roles WHERE rolname='ed_monitor_owner') THEN
   CREATE ROLE ed_monitor_owner NOLOGIN NOSUPERUSER BYPASSRLS;
 END IF;
END $$;
GRANT USAGE ON SCHEMA app TO ed_monitor,ed_monitor_owner;
GRANT SELECT ON app.jobs,app.runs,app.model_usage,app.provider_health,app.index_outbox,
 app.index_manifests,app.deletion_tombstones,app.workflow_threads,app.tool_executions TO ed_monitor_owner;
ALTER ROLE ed_monitor SET statement_timeout TO '2s';
ALTER ROLE ed_monitor SET default_transaction_read_only TO on;

CREATE FUNCTION app.operational_metrics() RETURNS jsonb
LANGUAGE sql STABLE SECURITY DEFINER SET search_path=pg_catalog,app AS $$
WITH usage AS (
 SELECT CASE WHEN model_key IN ('paid_standard','local_standard','paid_quality','paid_reference','anthropic_candidate','google_candidate') THEN model_key ELSE 'other' END AS model,
 usage_estimated, cost_micro_usd,input_tokens,output_tokens,state,execution_state,issued_at
 FROM app.model_usage
), metric_values AS (
 SELECT 'ed_jobs_queue_age_seconds' AS name,jsonb_build_object('kind',kind) AS labels,
 greatest(0,extract(epoch FROM now()-min(created_at)))::float8 AS value
 FROM app.jobs WHERE status IN ('queued','retry_wait') AND available_at<=now() GROUP BY kind
 UNION ALL SELECT 'ed_jobs_dead',jsonb_build_object('kind',kind),count(*)::float8
 FROM app.jobs WHERE status='dead' GROUP BY kind
 UNION ALL SELECT 'ed_active_runs',jsonb_build_object('state',status),count(*)::float8
 FROM app.runs WHERE status IN ('queued','running') GROUP BY status
 UNION ALL SELECT 'ed_index_outbox_age_seconds','{}'::jsonb,
 coalesce(greatest(0,extract(epoch FROM now()-min(created_at))),0)::float8
 FROM app.index_outbox WHERE state IN ('pending','leased')
 UNION ALL SELECT 'ed_index_outbox_dead','{}'::jsonb,count(*)::float8 FROM app.index_outbox WHERE state='dead'
 UNION ALL SELECT 'ed_index_manifest_unavailable','{}'::jsonb,count(*)::float8 FROM app.index_manifests WHERE state IN ('unavailable','failed')
 UNION ALL SELECT 'ed_source_purge_age_seconds','{}'::jsonb,
 coalesce(greatest(0,extract(epoch FROM now()-min(deleted_at))),0)::float8
 FROM app.deletion_tombstones WHERE purged_at IS NULL
 UNION ALL SELECT 'ed_graph_expired_active','{}'::jsonb,count(*)::float8 FROM app.workflow_threads
 WHERE status IN ('running','waiting') AND expires_at<now()
 UNION ALL SELECT 'ed_approval_overdue','{}'::jsonb,count(*)::float8 FROM app.tool_executions
 WHERE status IN ('proposed','awaiting_approval','approved','running') AND approval_expires_at<now()
 UNION ALL SELECT 'ed_ambiguous_usage_unsettled','{}'::jsonb,count(*)::float8 FROM usage WHERE state='reserved'
 AND (execution_state='ambiguous' OR (execution_state='issued' AND issued_at<now()-interval '60 seconds'))
 UNION ALL SELECT 'ed_model_cost_micro_usd',jsonb_build_object('model_key',model,'estimated',usage_estimated::text),coalesce(sum(cost_micro_usd),0)::float8
 FROM usage WHERE state IN ('settled','estimated') GROUP BY model,usage_estimated
 UNION ALL SELECT 'ed_model_tokens',jsonb_build_object('model_key',model,'direction','input'),coalesce(sum(input_tokens),0)::float8 FROM usage GROUP BY model
 UNION ALL SELECT 'ed_model_tokens',jsonb_build_object('model_key',model,'direction','output'),coalesce(sum(output_tokens),0)::float8 FROM usage GROUP BY model
 UNION ALL SELECT 'ed_provider_breaker_state',jsonb_build_object('model_key',CASE WHEN model_key IN ('paid_standard','local_standard','paid_quality','paid_reference','anthropic_candidate','google_candidate') THEN model_key ELSE 'other' END),
 max(CASE state WHEN 'closed' THEN 0 WHEN 'half_open' THEN 1 WHEN 'open' THEN 2 ELSE 3 END)::float8
 FROM app.provider_health GROUP BY 2
 UNION ALL SELECT 'ed_provider_probe_age_seconds',jsonb_build_object('model_key',CASE WHEN model_key IN ('paid_standard','local_standard','paid_quality','paid_reference','anthropic_candidate','google_candidate') THEN model_key ELSE 'other' END),
 coalesce(max(greatest(0,extract(epoch FROM now()-last_probe_at))),86400)::float8
 FROM app.provider_health GROUP BY 2
)
SELECT coalesce(jsonb_agg(jsonb_build_object('name',name,'labels',labels,'value',value) ORDER BY name,labels::text),'[]'::jsonb) FROM metric_values;
$$;
ALTER FUNCTION app.operational_metrics() OWNER TO ed_monitor_owner;
REVOKE ALL ON FUNCTION app.operational_metrics() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION app.operational_metrics() TO ed_monitor;
