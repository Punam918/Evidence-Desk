"""Global authentication audit has no artificial workspace association."""

from pathlib import Path

from evidencedesk.db.migrations import execute_sql

revision = "0008_identity_audit"
down_revision = "0007_admission"
branch_labels = None
depends_on = None


def upgrade() -> None:
    execute_sql((Path(__file__).parents[1] / "sql/0008_identity_audit.sql").read_text())


def downgrade() -> None:
    raise RuntimeError("Audit history must be preserved by forward migrations.")
