"""Restricted identity lookup and one-use browser login state."""

from pathlib import Path

from evidencedesk.db.migrations import execute_sql

revision = "0003_identity"
down_revision = "0002_roles"
branch_labels = None
depends_on = None


def upgrade() -> None:
    execute_sql((Path(__file__).parents[1] / "sql/0003_identity.sql").read_text())


def downgrade() -> None:
    raise RuntimeError("Use an explicit forward migration for identity changes.")
