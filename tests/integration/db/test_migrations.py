from pathlib import Path
from uuid import uuid4

import psycopg
import pytest
from evidencedesk.db.session import AuthContext, engine, tenant_transaction
from sqlalchemy import text

pytestmark = pytest.mark.integration


def test_reference_negative_invariants(database):
    with psycopg.connect(database["operator"], autocommit=True) as conn:
        sql = Path("scripts/schema-invariants.sql").read_text()
        sql = sql.replace(
            "384,true,'fixture-config',now());",
            "384,true,'fixture-config',now()) ON CONFLICT DO NOTHING;",
        )
        conn.execute(sql, prepare=False)


def test_runtime_privilege_separation(database):
    for role in ["ed_app", "ed_identity", "ed_dispatch", "ed_checkpoint"]:
        with psycopg.connect(database[role], autocommit=True) as conn:
            assert conn.execute(
                "SELECT rolsuper,rolbypassrls FROM pg_roles WHERE rolname=current_user"
            ).fetchone() == (False, False)
            for statement in ["SELECT * FROM app.sessions", "CREATE TABLE app.unsafe(id int)"]:
                with pytest.raises(psycopg.errors.InsufficientPrivilege):
                    conn.execute(statement)
            if role != "ed_checkpoint":
                with pytest.raises(psycopg.errors.InsufficientPrivilege):
                    conn.execute("SELECT * FROM lg_checkpoints.checkpoints")
            else:
                assert conn.execute(
                    "SELECT count(*) >= 0 FROM lg_checkpoints.checkpoints"
                ).fetchone() == (True,)


async def test_scope_does_not_leak_from_pool(database):
    db = engine(database["ed_app"].replace("postgresql://", "postgresql+psycopg://"), pool_size=1)
    try:
        for _ in range(2):
            workspace = uuid4()
            context = AuthContext(
                user_id=uuid4(),
                workspace_id=workspace,
                request_id=uuid4(),
                role="viewer",
                policy_revision=1,
            )
            async with tenant_transaction(db, context) as conn:
                assert (
                    await conn.execute(text("SELECT app.workspace_scope()"))
                ).scalar_one() == workspace
            async with db.connect() as conn:
                assert (
                    await conn.execute(text("SELECT app.workspace_scope()"))
                ).scalar_one() is None
    finally:
        await db.dispose()
