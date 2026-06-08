"""Indexes for bounded expiration of tenant replay data."""

from evidencedesk.db.migrations import execute_sql

revision = "0021_replay_retention"
down_revision = "0020_checkpoint_retention"
branch_labels = None
depends_on = None


def upgrade() -> None:
    execute_sql("""
        CREATE INDEX idempotency_expiry ON app.idempotency_keys(workspace_id,expires_at);
        CREATE INDEX run_retention ON app.runs(workspace_id,created_at,id)
          WHERE status NOT IN ('queued','running');
        CREATE INDEX usage_response_retention ON app.model_usage(workspace_id,created_at,id)
          WHERE response IS NOT NULL AND state IN ('settled','estimated');
    """)


def downgrade() -> None:
    execute_sql("""
        DROP INDEX app.usage_response_retention;
        DROP INDEX app.run_retention;
        DROP INDEX app.idempotency_expiry;
    """)
