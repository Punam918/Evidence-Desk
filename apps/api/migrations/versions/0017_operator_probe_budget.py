"""Conservative UTC operator probe reservations survive process death."""

from evidencedesk.db.migrations import execute_sql

revision = "0017_operator_probe_budget"
down_revision = "0016_provider_recovery"
branch_labels = None
depends_on = None


def upgrade() -> None:
    execute_sql("""
    ALTER TABLE app.provider_health
      ADD COLUMN probe_budget_day date,
      ADD COLUMN probe_reserved_micro_usd bigint NOT NULL DEFAULT 0
        CHECK(probe_reserved_micro_usd>=0 AND probe_reserved_micro_usd<=1000000);
    """)


def downgrade() -> None:
    raise RuntimeError("Use a forward migration; do not discard probe reservations.")
