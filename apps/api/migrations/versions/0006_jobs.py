"""Fenced job dispatch, index outbox and conservative issued-attempt recovery."""

from pathlib import Path

from evidencedesk.db.migrations import execute_sql

revision = "0006_jobs"
down_revision = "0005_tenancy"
branch_labels = None
depends_on = None


def upgrade() -> None:
    execute_sql((Path(__file__).parents[1] / "sql/0006_jobs.sql").read_text())


def downgrade() -> None:
    raise RuntimeError("Use a forward migration for active job protocols.")
