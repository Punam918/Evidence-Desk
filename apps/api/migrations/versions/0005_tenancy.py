"""Identity-scoped creation and restricted member email lookup."""

from pathlib import Path

from evidencedesk.db.migrations import execute_sql

revision = "0005_tenancy"
down_revision = "0004_profile"
branch_labels = None
depends_on = None


def upgrade() -> None:
    execute_sql((Path(__file__).parents[1] / "sql/0005_tenancy.sql").read_text())


def downgrade() -> None:
    raise RuntimeError("Use forward migration for permission changes.")
