"""Separate least-privilege runtime and function-owner roles."""

from pathlib import Path

from evidencedesk.db.migrations import execute_sql

revision = "0002_roles"
down_revision = "0001_business"
branch_labels = None
depends_on = None


def upgrade() -> None:
    execute_sql((Path(__file__).parents[1] / "sql/0002_roles.sql").read_text())


def downgrade() -> None:
    raise RuntimeError("Role revocation requires an explicit forward migration.")
