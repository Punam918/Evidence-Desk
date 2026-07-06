"""Exercise fresh and repeat bootstrap against an isolated real PostgreSQL."""

import os
import subprocess
import time
from uuid import uuid4

import psycopg
import pytest

from infra.bootstrap.database import assert_runtime_roles
from infra.bootstrap.database import initialize as initialize_database
from infra.bootstrap.entrypoint import database_url, password
from infra.bootstrap.secrets import DATABASE_ROLES
from infra.bootstrap.secrets import initialize as initialize_secrets

PG_IMAGE = "postgres@sha256:74935e72241653ca55e0414067e6d8763aceb8a810eb51b452253ec3dcfc4336"
pytestmark = pytest.mark.integration


def test_fresh_database_bootstrap_repeats_without_changing_privilege_boundary(tmp_path):
    root = tmp_path / "secrets"
    initialize_secrets(root, uid=os.getuid(), gid=os.getgid(), profile_owners={})
    operator_password = password(root / "operator", "postgres_password")
    name = "ed-bootstrap-test-" + uuid4().hex[:12]
    # File content goes through stdin; credentials are absent from command arguments/output.
    subprocess.run(
        [
            "docker",
            "run",
            "--rm",
            "-d",
            "--name",
            name,
            "--memory",
            "1g",
            "--tmpfs",
            "/var/lib/postgresql",
            "-p",
            "127.0.0.1::5432",
            "--env-file",
            "/dev/stdin",
            PG_IMAGE,
        ],
        input=f"POSTGRES_PASSWORD={operator_password}\n".encode(),
        check=True,
        capture_output=True,
    )
    try:
        port = int(
            subprocess.check_output(["docker", "port", name, "5432/tcp"], text=True)
            .strip()
            .rsplit(":", 1)[1]
        )
        operator = database_url("postgres", operator_password, "127.0.0.1", port, "postgres")
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            try:
                with psycopg.connect(operator, connect_timeout=1):
                    break
            except psycopg.OperationalError:
                time.sleep(0.1)
        else:
            pytest.fail("Isolated bootstrap database did not start")

        initialize_database(root / "operator", host="127.0.0.1", port=port)
        app_operator = database_url(
            "postgres", operator_password, "127.0.0.1", port, "evidencedesk"
        )
        with psycopg.connect(app_operator) as conn:
            before = conn.execute("SELECT version_num FROM alembic_version").fetchone()
            assert before == ("0022_conversation_purge",)
            saver_before = conn.execute(
                "SELECT max(v) FROM lg_checkpoints.checkpoint_migrations"
            ).fetchone()
            assert saver_before and saver_before[0] is not None
            # A user relation in the Keycloak-owned DB proves reinitialization preserves data.
        keycloak = database_url(
            "ed_keycloak",
            password(root / "operator", "keycloak_password"),
            "127.0.0.1",
            port,
            "keycloak",
        )
        with psycopg.connect(keycloak) as conn:
            conn.execute("CREATE TABLE bootstrap_sentinel(value text PRIMARY KEY)")
            conn.execute("INSERT INTO bootstrap_sentinel VALUES('retained')")

        initialize_database(root / "operator", host="127.0.0.1", port=port)
        with psycopg.connect(app_operator) as conn:
            assert conn.execute("SELECT version_num FROM alembic_version").fetchone() == before
            assert (
                conn.execute("SELECT max(v) FROM lg_checkpoints.checkpoint_migrations").fetchone()
                == saver_before
            )
            assert conn.execute(
                "SELECT bool_and(NOT rolsuper AND NOT rolbypassrls AND NOT rolcreatedb AND NOT rolcreaterole AND NOT rolreplication) FROM pg_roles WHERE rolname=ANY(%s)",
                (list(DATABASE_ROLES),),
            ).fetchone() == (True,)
            assert conn.execute(
                "SELECT count(*) FROM pg_auth_members m JOIN pg_roles r ON r.oid=m.member WHERE r.rolname=ANY(%s)",
                (list(DATABASE_ROLES),),
            ).fetchone() == (0,)
        with psycopg.connect(keycloak) as conn:
            assert conn.execute("SELECT value FROM bootstrap_sentinel").fetchone() == ("retained",)

        for role in DATABASE_ROLES:
            secret = password(root / "operator", f"{role}_password")
            scoped = database_url(role, secret, "127.0.0.1", port, "evidencedesk")
            with psycopg.connect(scoped, autocommit=True) as conn:
                assert conn.execute("SELECT current_user").fetchone() == (role,)
                with pytest.raises(
                    (psycopg.errors.InsufficientPrivilege, psycopg.errors.ReadOnlySqlTransaction)
                ):
                    conn.execute("CREATE TABLE app.forbidden_test(id integer)")
            blocked = database_url(role, secret, "127.0.0.1", port, "keycloak")
            with pytest.raises(psycopg.OperationalError):
                psycopg.connect(blocked).close()
        forbidden = database_url(
            "ed_keycloak",
            password(root / "operator", "keycloak_password"),
            "127.0.0.1",
            port,
            "evidencedesk",
        )
        with pytest.raises(psycopg.OperationalError):
            psycopg.connect(forbidden).close()

        app = database_url(
            "ed_app",
            password(root / "operator", "ed_app_password"),
            "127.0.0.1",
            port,
            "evidencedesk",
        )
        with psycopg.connect(app, autocommit=True) as conn:
            # Existing FORCE RLS remains active; no synthetic session scope is granted.
            assert conn.execute("SELECT count(*) FROM app.workspaces").fetchone() == (0,)
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                conn.execute("SELECT * FROM lg_checkpoints.checkpoints")
        checkpoint = database_url(
            "ed_checkpoint",
            password(root / "operator", "ed_checkpoint_password"),
            "127.0.0.1",
            port,
            "evidencedesk",
        )
        with psycopg.connect(checkpoint, autocommit=True) as conn:
            assert conn.execute("SELECT count(*) FROM checkpoints").fetchone() == (0,)
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                conn.execute("SELECT * FROM app.workspaces")
        with psycopg.connect(app_operator, autocommit=True) as conn:
            conn.execute("ALTER ROLE ed_app BYPASSRLS")
            with pytest.raises(ValueError, match="privilege boundary"):
                assert_runtime_roles(conn)
    finally:
        subprocess.run(["docker", "rm", "-f", name], check=False, capture_output=True)
