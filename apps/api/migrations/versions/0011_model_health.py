"""API reads readiness; only the internal dispatcher/operator records probes."""

from evidencedesk.db.migrations import execute_sql

revision = "0011_model_health"
down_revision = "0010_usage_prices"
branch_labels = None
depends_on = None


def upgrade() -> None:
    execute_sql(
        "GRANT SELECT ON app.provider_health TO ed_app; GRANT SELECT,INSERT,UPDATE ON app.provider_health TO ed_dispatch"
    )


def downgrade() -> None:
    raise RuntimeError("Use a forward migration for role changes.")
