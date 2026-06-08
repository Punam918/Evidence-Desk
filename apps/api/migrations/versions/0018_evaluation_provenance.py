"""Report source identity need not invent a Git commit in an unpacked workspace."""

from evidencedesk.db.migrations import execute_sql

revision = "0018_evaluation_provenance"
down_revision = "0017_operator_probe_budget"
branch_labels = None
depends_on = None


def upgrade() -> None:
    execute_sql("""
    ALTER TABLE app.evaluation_runs
      ALTER COLUMN git_sha DROP NOT NULL,
      ADD COLUMN source_snapshot_sha256 text,
      ADD COLUMN evidence_scope text NOT NULL DEFAULT 'legacy_unclassified'
        CHECK(evidence_scope IN ('draft_diagnostic','reviewed_benchmark','operational','legacy_unclassified')),
      ADD CONSTRAINT evaluation_snapshot_hash CHECK(source_snapshot_sha256 ~ '^[a-f0-9]{64}$'),
      ADD CONSTRAINT evaluation_provenance_required CHECK(evidence_scope='legacy_unclassified' OR source_snapshot_sha256 IS NOT NULL);
    """)


def downgrade() -> None:
    raise RuntimeError("Use a forward migration; preserve report provenance.")
