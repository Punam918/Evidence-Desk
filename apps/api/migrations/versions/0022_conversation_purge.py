"""Scoped conversation-purge dispatch and surviving checkpoint tombstones."""

from pathlib import Path

from evidencedesk.db.migrations import execute_sql

revision = "0022_conversation_purge"
down_revision = "0021_replay_retention"
branch_labels = None
depends_on = None


def upgrade() -> None:
    execute_sql((Path(__file__).parents[1] / "sql/0022_conversation_purge.sql").read_text())


def downgrade() -> None:
    raise RuntimeError("Use a reviewed forward migration for deletion and dispatch state.")
