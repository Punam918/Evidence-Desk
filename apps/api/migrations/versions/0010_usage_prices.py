"""Persist immutable attempt prices for recovery across operator registry changes."""

from pathlib import Path

from evidencedesk.db.migrations import execute_sql

revision = "0010_usage_prices"
down_revision = "0009_run_admission_lock"
branch_labels = None
depends_on = None


def upgrade() -> None:
    execute_sql(
        "ALTER TABLE app.model_usage ADD COLUMN price_snapshot jsonb, ADD COLUMN inference_seconds double precision CHECK(inference_seconds>=0)"
    )

    execute_sql((Path(__file__).parents[1] / "sql/0010_usage_recovery.sql").read_text())


def downgrade() -> None:
    raise RuntimeError("Usage snapshots require a forward migration.")
