"""Immutable initial business schema; mirrors reviewed v2 contract."""

from pathlib import Path

from evidencedesk.db.migrations import execute_sql

revision = "0001_business"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    sql = (Path(__file__).parents[1] / "sql/0001_business.sql").read_text()
    execute_sql(sql)


def downgrade() -> None:
    raise RuntimeError("Destructive downgrade is not supported. Restore a tested backup.")
