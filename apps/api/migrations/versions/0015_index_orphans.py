"""Durable metadata-only quarantine for unknown derived points."""

from evidencedesk.db.migrations import execute_sql

revision = "0015_index_orphans"
down_revision = "0014_index_renewal"
branch_labels = None
depends_on = None


def upgrade() -> None:
    execute_sql("""
    CREATE UNIQUE INDEX chunks_global_id ON app.chunks(id);
    CREATE TABLE app.index_orphans (
      workspace_id uuid NOT NULL REFERENCES app.workspaces ON DELETE CASCADE,
      collection_name text NOT NULL, point_id text NOT NULL CHECK(length(point_id) BETWEEN 1 AND 128),
      payload_sha256 text NOT NULL CHECK(payload_sha256 ~ '^[a-f0-9]{64}$'),
      first_seen timestamptz NOT NULL DEFAULT now(), last_seen timestamptz NOT NULL DEFAULT now(),
      PRIMARY KEY(workspace_id,collection_name,point_id)
    );
    ALTER TABLE app.index_orphans ENABLE ROW LEVEL SECURITY;
    ALTER TABLE app.index_orphans FORCE ROW LEVEL SECURITY;
    CREATE POLICY tenant ON app.index_orphans USING(workspace_id=app.workspace_scope())
      WITH CHECK(workspace_id=app.workspace_scope());
    GRANT SELECT,INSERT,UPDATE,DELETE ON app.index_orphans TO ed_app;
    GRANT SELECT ON app.chunks TO ed_dispatch_owner;
    CREATE FUNCTION app.known_chunk_ids(p_ids uuid[]) RETURNS SETOF uuid
    LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,app AS $$
    BEGIN
      IF cardinality(p_ids)>100 THEN RAISE EXCEPTION 'bounded batch required'; END IF;
      RETURN QUERY SELECT c.id FROM app.chunks c WHERE c.id=ANY(p_ids);
    END $$;
    ALTER FUNCTION app.known_chunk_ids(uuid[]) OWNER TO ed_dispatch_owner;
    REVOKE ALL ON FUNCTION app.known_chunk_ids(uuid[]) FROM PUBLIC;
    GRANT EXECUTE ON FUNCTION app.known_chunk_ids(uuid[]) TO ed_dispatch;
    """)


def downgrade() -> None:
    raise RuntimeError("Use a forward migration for derived-index quarantine changes.")
