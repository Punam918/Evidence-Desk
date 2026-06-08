"""Retain checkpoint deletion receipts and fence later graph invocations."""

from evidencedesk.db.migrations import execute_sql

revision = "0020_checkpoint_retention"
down_revision = "0019_operational_metrics"
branch_labels = None
depends_on = None


def upgrade() -> None:
    execute_sql("""
        ALTER TABLE app.workflow_threads ADD COLUMN checkpoint_purged_at timestamptz;
        CREATE INDEX workflow_retention_due ON app.workflow_threads(workspace_id,expires_at,id);
    """)


def downgrade() -> None:
    raise RuntimeError("Checkpoint deletion receipts require a reviewed forward migration.")
