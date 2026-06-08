"""Serialize global and tenant ingestion queue admission."""

from pathlib import Path

from evidencedesk.db.migrations import execute_sql

revision = "0012_ingest_admission"
down_revision = "0011_model_health"
branch_labels = None
depends_on = None


def upgrade() -> None:
    execute_sql((Path(__file__).parent.parent / "sql/0012_ingest_admission.sql").read_text())


def downgrade() -> None:
    raise RuntimeError("Use a forward migration for admission changes.")
