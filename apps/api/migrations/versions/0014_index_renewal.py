"""Renew derived-index delivery leases without granting dispatcher content access."""

from evidencedesk.db.migrations import execute_sql

revision = "0014_index_renewal"
down_revision = "0013_ingestion_recovery"
branch_labels = None
depends_on = None


def upgrade() -> None:
    execute_sql("""
    CREATE FUNCTION app.renew_index_outbox(p_workspace uuid,p_id uuid,p_token bigint)
    RETURNS boolean LANGUAGE sql SECURITY DEFINER SET search_path=pg_catalog,app AS $$
      WITH renewed AS (UPDATE app.index_outbox SET lease_until=clock_timestamp()+interval '30 seconds'
      WHERE workspace_id=p_workspace AND id=p_id AND lease_token=p_token
      AND state='leased' AND lease_until>clock_timestamp() RETURNING 1)
      SELECT EXISTS(SELECT 1 FROM renewed);
    $$;
    ALTER FUNCTION app.renew_index_outbox(uuid,uuid,bigint) OWNER TO ed_dispatch_owner;
    REVOKE ALL ON FUNCTION app.renew_index_outbox(uuid,uuid,bigint) FROM PUBLIC;
    GRANT EXECUTE ON FUNCTION app.renew_index_outbox(uuid,uuid,bigint) TO ed_dispatch;
    """)


def downgrade() -> None:
    raise RuntimeError("Use a forward migration for index delivery changes.")
