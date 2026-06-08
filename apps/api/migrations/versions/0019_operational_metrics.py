"""Dedicated monitor role sees only fixed global aggregates, never tenant content."""

from pathlib import Path

from evidencedesk.db.migrations import execute_sql

revision = "0019_operational_metrics"
down_revision = "0018_evaluation_provenance"
branch_labels = None
depends_on = None


def upgrade() -> None:
    execute_sql((Path(__file__).parents[1] / "sql/0019_operational_metrics.sql").read_text())


def downgrade() -> None:
    raise RuntimeError("Use a forward migration for monitoring privileges.")
