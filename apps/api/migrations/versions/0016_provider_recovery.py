"""Durable retry classification and shared rolling breaker/probe leases."""

from evidencedesk.db.migrations import execute_sql

revision = "0016_provider_recovery"
down_revision = "0015_index_orphans"
branch_labels = None
depends_on = None


def upgrade() -> None:
    execute_sql("""
    ALTER TABLE app.model_usage
      ADD COLUMN failure_code text CHECK(length(failure_code)<=80),
      ADD COLUMN failure_retryable boolean NOT NULL DEFAULT false,
      ADD COLUMN retry_after_seconds double precision CHECK(retry_after_seconds>=0 AND retry_after_seconds<'Infinity'::float8),
      ADD COLUMN health_recorded boolean NOT NULL DEFAULT false;
    ALTER TABLE app.provider_health
      ADD COLUMN failure_times timestamptz[] NOT NULL DEFAULT '{}' CHECK(cardinality(failure_times)<=5),
      ADD COLUMN probe_token uuid,
      ADD COLUMN probe_until timestamptz;
    CREATE FUNCTION app.claim_provider_health_outcome(p_workspace uuid,p_attempt uuid)
    RETURNS TABLE(model_key text,price_version text,failure_code text,failure_retryable boolean)
    LANGUAGE sql SECURITY DEFINER SET search_path=pg_catalog,app AS $$
      UPDATE app.model_usage u SET health_recorded=true
      WHERE u.workspace_id=p_workspace AND u.id=p_attempt AND u.state<>'reserved' AND NOT u.health_recorded
      RETURNING u.model_key,u.price_version,u.failure_code,u.failure_retryable
    $$;
    ALTER FUNCTION app.claim_provider_health_outcome(uuid,uuid) OWNER TO ed_dispatch_owner;
    REVOKE ALL ON FUNCTION app.claim_provider_health_outcome(uuid,uuid) FROM PUBLIC;
    GRANT EXECUTE ON FUNCTION app.claim_provider_health_outcome(uuid,uuid) TO ed_dispatch;
    """)


def downgrade() -> None:
    raise RuntimeError("Use a forward migration for provider recovery changes.")
