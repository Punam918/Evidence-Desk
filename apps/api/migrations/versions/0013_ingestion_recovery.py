"""Keep document stage consistent when ingestion leases expire."""

from pathlib import Path

from evidencedesk.db.migrations import execute_sql

revision = "0013_ingestion_recovery"
down_revision = "0012_ingest_admission"
branch_labels = None
depends_on = None


def upgrade() -> None:
    execute_sql((Path(__file__).parent.parent / "sql/0013_ingestion_recovery.sql").read_text())


def downgrade() -> None:
    raise RuntimeError("Use a forward migration for recovery changes.")
