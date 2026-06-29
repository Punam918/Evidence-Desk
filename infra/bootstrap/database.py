"""One-shot, serialized database/bootstrap job; runtime never receives this credential."""

import argparse
import os
import subprocess
import sys
from pathlib import Path

import psycopg
from psycopg import sql

from infra.bootstrap.entrypoint import database_url, password
from infra.bootstrap.secrets import DATABASE_ROLES

ROOT = Path(__file__).resolve().parents[2]
BOOTSTRAP_LOCK = 814326002


def ensure_database(connection: psycopg.Connection, name: str, owner: str) -> None:
    row = connection.execute(
        "SELECT pg_get_userbyid(datdba) FROM pg_database WHERE datname=%s", (name,)
    ).fetchone()
    if row is None:
        connection.execute(
            sql.SQL("CREATE DATABASE {} OWNER {}").format(
                sql.Identifier(name), sql.Identifier(owner)
            )
        )
    elif row[0] != owner:
        raise ValueError("Bootstrap database has an unexpected owner")
    connection.execute(
        sql.SQL("REVOKE ALL ON DATABASE {} FROM PUBLIC").format(sql.Identifier(name))
    )


def ensure_keycloak_role(connection: psycopg.Connection, secret: str) -> None:
    exists = connection.execute("SELECT 1 FROM pg_roles WHERE rolname='ed_keycloak'").fetchone()
    if not exists:
        connection.execute(
            "CREATE ROLE ed_keycloak LOGIN NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE NOREPLICATION"
        )
    connection.execute(
        sql.SQL(
            "ALTER ROLE ed_keycloak LOGIN NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE NOREPLICATION PASSWORD {}"
        ).format(sql.Literal(secret))
    )


def assert_runtime_roles(connection: psycopg.Connection) -> None:
    roles = (*DATABASE_ROLES, "ed_keycloak")
    rows = connection.execute(
        "SELECT rolname,rolsuper,rolbypassrls,rolcreatedb,rolcreaterole,rolreplication,rolcanlogin "
        "FROM pg_roles WHERE rolname=ANY(%s)",
        (list(roles),),
    ).fetchall()
    if len(rows) != len(roles) or any(any(row[1:6]) or not row[6] for row in rows):
        raise ValueError("Runtime role attributes violate the privilege boundary")
    inherited = connection.execute(
        "SELECT 1 FROM pg_auth_members m JOIN pg_roles r ON r.oid=m.member "
        "WHERE r.rolname=ANY(%s) LIMIT 1",
        (list(roles),),
    ).fetchone()
    if inherited:
        raise ValueError("Runtime roles must not inherit other roles")
    owner = connection.execute(
        "SELECT 1 FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace "
        "JOIN pg_roles r ON r.oid=c.relowner "
        "WHERE n.nspname IN ('app','lg_checkpoints') AND r.rolname=ANY(%s) LIMIT 1",
        (list(roles),),
    ).fetchone()
    if owner:
        raise ValueError("Runtime roles must not own application/checkpoint relations")
    schema_owner = connection.execute(
        "SELECT 1 FROM pg_namespace n JOIN pg_roles r ON r.oid=n.nspowner "
        "WHERE n.nspname IN ('app','lg_checkpoints') AND r.rolname=ANY(%s) LIMIT 1",
        (list(roles),),
    ).fetchone()
    if schema_owner:
        raise ValueError("Runtime roles must not own application/checkpoint schemas")


def run_migrations(dsn: str) -> None:
    environment = {**os.environ, "ED_MIGRATION_DATABASE_URL": dsn}
    commands = (
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        [sys.executable, "scripts/setup_checkpoints.py"],
    )
    for command in commands:
        # Do not forward driver exceptions: they may include SQL parameters or a DSN.
        result = subprocess.run(
            command, cwd=ROOT, env=environment, capture_output=True, timeout=240
        )
        if result.returncode:
            raise RuntimeError("A schema bootstrap stage failed")


def initialize(root: Path, *, host: str = "postgres", port: int = 5432) -> None:
    operator_password = password(root, "postgres_password")
    maintenance = database_url("postgres", operator_password, host, port, "postgres")
    application = database_url("postgres", operator_password, host, port, "evidencedesk")
    with psycopg.connect(maintenance, autocommit=True, connect_timeout=10) as operator:
        operator.execute("SELECT pg_advisory_lock(%s)", (BOOTSTRAP_LOCK,))
        try:
            ensure_keycloak_role(operator, password(root, "keycloak_password"))
            ensure_database(operator, "keycloak", "ed_keycloak")
            ensure_database(operator, "evidencedesk", "postgres")
            run_migrations(application)
            with psycopg.connect(application, autocommit=False, connect_timeout=10) as conn:
                assert_runtime_roles(conn)
                for role in DATABASE_ROLES:
                    conn.execute(
                        sql.SQL("ALTER ROLE {} PASSWORD {}").format(
                            sql.Identifier(role), sql.Literal(password(root, f"{role}_password"))
                        )
                    )
                    conn.execute(
                        sql.SQL("GRANT CONNECT ON DATABASE evidencedesk TO {}").format(
                            sql.Identifier(role)
                        )
                    )
                conn.execute("REVOKE CREATE ON SCHEMA public FROM PUBLIC")
                conn.execute(
                    "REVOKE ALL ON DATABASE keycloak FROM ed_app,ed_identity,ed_dispatch,ed_checkpoint,ed_monitor"
                )
                conn.execute("REVOKE ALL ON DATABASE evidencedesk FROM ed_keycloak")
                conn.execute("REVOKE ALL ON DATABASE postgres FROM PUBLIC")
        finally:
            operator.execute("SELECT pg_advisory_unlock(%s)", (BOOTSTRAP_LOCK,))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--secrets", type=Path, default=Path("/run/secrets"))
    parser.add_argument("--host", default="postgres")
    parser.add_argument("--port", type=int, default=5432)
    args = parser.parse_args()
    try:
        initialize(args.secrets, host=args.host, port=args.port)
    except Exception:
        raise SystemExit(
            "Database initialization failed; review bootstrap state and migration compatibility."
        ) from None
    print("Business schema, checkpoint schema and scoped database roles initialized.")


if __name__ == "__main__":
    main()
