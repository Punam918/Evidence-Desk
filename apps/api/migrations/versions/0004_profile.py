"""Immutable pinned embedding profile."""

from pathlib import Path

from evidencedesk.db.migrations import execute_sql

revision = "0004_profile"
down_revision = "0003_identity"
branch_labels = None
depends_on = None


def upgrade() -> None:
    execute_sql((Path(__file__).parents[1] / "sql/0004_profile.sql").read_text())


def downgrade() -> None:
    raise RuntimeError("Published profiles require forward migration.")
