"""Load file-backed role credentials without putting them in Compose's environment."""

import argparse
import os
import re
from pathlib import Path
from urllib.parse import quote

from infra.bootstrap.secrets import read_private

PROFILE_ROLES = {
    "api": ("ed_app", "ed_identity"),
    "ingest": ("ed_app", "ed_dispatch"),
    "index": ("ed_app", "ed_dispatch"),
    "chat": ("ed_app", "ed_identity", "ed_dispatch", "ed_checkpoint"),
    "brief": ("ed_app", "ed_dispatch", "ed_checkpoint"),
    "probe": ("ed_app", "ed_dispatch"),
    "metrics": ("ed_monitor",),
}
ROLE_VARIABLES = {
    "ed_app": "ED_DATABASE_URL",
    "ed_identity": "ED_IDENTITY_DATABASE_URL",
    "ed_dispatch": "ED_DISPATCH_DATABASE_URL",
    "ed_checkpoint": "ED_CHECKPOINT_DATABASE_URL",
    "ed_monitor": "ED_MONITOR_DATABASE_URL",
}


def password(root: Path, name: str) -> str:
    value = read_private(root / name).decode("ascii").strip()
    if not re.fullmatch(r"[a-f0-9]{64}", value):
        raise ValueError("Database credential has an unexpected format")
    return value


def database_url(role: str, secret: str, host: str, port: int, database: str) -> str:
    # Keep host/db operator configuration from injecting DSN options or credentials.
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.-]*", host) or not 1 <= port <= 65535:
        raise ValueError("Invalid database address")
    if not re.fullmatch(r"[a-z][a-z0-9_]*", database):
        raise ValueError("Invalid database name")
    return f"postgresql://{quote(role, safe='')}:{quote(secret, safe='')}@{host}:{port}/{database}"


def runtime_environment(profile: str, root: Path, inherited: dict[str, str]) -> dict[str, str]:
    forbidden = {*ROLE_VARIABLES.values(), "ED_MIGRATION_DATABASE_URL"}
    environment = {k: v for k, v in inherited.items() if k not in forbidden}
    host = environment.get("ED_DB_HOST", "postgres")
    port = int(environment.get("ED_DB_PORT", "5432"))
    database = environment.get("ED_DB_NAME", "evidencedesk")
    for role in PROFILE_ROLES[profile]:
        environment[ROLE_VARIABLES[role]] = database_url(
            role, password(root, f"{role}_password"), host, port, database
        )
    return environment


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", required=True, choices=tuple(PROFILE_ROLES))
    parser.add_argument("--secrets", type=Path, default=Path("/run/secrets"))
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    if not command:
        parser.error("A process command is required")
    try:
        environment = runtime_environment(args.profile, args.secrets, dict(os.environ))
        os.execvpe(command[0], command, environment)
    except Exception:
        raise SystemExit(
            "Runtime bootstrap failed; check mounted role credentials and command."
        ) from None


if __name__ == "__main__":
    main()
