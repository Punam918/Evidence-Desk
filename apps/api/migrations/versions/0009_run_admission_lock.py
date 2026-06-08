"""Expose a narrow transaction lock so run idempotency precedes slot reservation."""

from evidencedesk.db.migrations import execute_sql

revision = "0009_run_admission_lock"
down_revision = "0008_identity_audit"
branch_labels = None
depends_on = None


def upgrade() -> None:
    execute_sql("""CREATE FUNCTION app.lock_run_admission() RETURNS void LANGUAGE sql SECURITY DEFINER SET search_path=pg_catalog,app AS $$
        SELECT singleton FROM app.admission_counters WHERE singleton FOR UPDATE;
        $$;
        ALTER FUNCTION app.lock_run_admission() OWNER TO ed_dispatch_owner;
        REVOKE ALL ON FUNCTION app.lock_run_admission() FROM PUBLIC;
        GRANT EXECUTE ON FUNCTION app.lock_run_admission() TO ed_app;
        CREATE FUNCTION app.reconcile_run_slots() RETURNS void LANGUAGE sql SECURITY DEFINER SET search_path=pg_catalog,app AS $$
        UPDATE app.admission_counters SET active_runs=(SELECT count(*) FROM app.runs WHERE status IN ('queued','running')),updated_at=clock_timestamp() WHERE singleton;
        $$;
        ALTER FUNCTION app.reconcile_run_slots() OWNER TO ed_dispatch_owner;
        REVOKE ALL ON FUNCTION app.reconcile_run_slots() FROM PUBLIC;
        GRANT EXECUTE ON FUNCTION app.reconcile_run_slots() TO ed_app;""")


def downgrade() -> None:
    raise RuntimeError("Use forward migration for admission protocols.")
