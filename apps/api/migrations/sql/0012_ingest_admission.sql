CREATE FUNCTION app.check_ingest_admission(p_workspace uuid) RETURNS boolean
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,app AS $$
BEGIN
 IF p_workspace::text IS DISTINCT FROM current_setting('app.workspace_id',true) THEN
  RAISE EXCEPTION 'invalid tenant scope' USING ERRCODE='42501';
 END IF;
 -- Held until the caller commits its revision/job creation. No object I/O in this transaction.
 PERFORM pg_advisory_xact_lock(hashtextextended('ed-ingest-admission',0));
 RETURN (SELECT count(*)<100 FROM app.jobs WHERE kind='ingest' AND status IN ('queued','running','retry_wait'))
    AND (SELECT count(*)<10 FROM app.jobs WHERE workspace_id=p_workspace AND kind='ingest' AND status IN ('queued','running','retry_wait'));
END $$;
ALTER FUNCTION app.check_ingest_admission(uuid) OWNER TO ed_dispatch_owner;
REVOKE ALL ON FUNCTION app.check_ingest_admission(uuid) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION app.check_ingest_admission(uuid) TO ed_app;
