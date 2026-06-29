"""Generate persistent local credentials and distribute only each service's subset.

The one-shot init container owns the complete volume. Serving containers mount a
single profile subpath, read-only; mounting this entire volume into a runtime
service would defeat the separation. No credential is returned on stdout.
"""

import argparse
import fcntl
import os
import re
import secrets
import stat
import tempfile
from pathlib import Path

RUNTIME_UID = 10001
RUNTIME_GID = 10001
PROFILE_OWNERS = {"postgres": (999, 999)}
DATABASE_ROLES = ("ed_app", "ed_identity", "ed_dispatch", "ed_checkpoint", "ed_monitor")
SECRET_LENGTHS = {
    "postgres_password": 64,
    **{f"{role}_password": 64 for role in DATABASE_ROLES},
    "keycloak_password": 64,
    "keycloak_admin_password": 64,
    "oidc_client_secret": 64,
    "demo_password": 48,
    "session_key": 64,
    "cursor_key": 64,
    "s3_admin_access_key": 20,
    "s3_admin_secret_key": 64,
    "s3_access_key": 20,
    "s3_secret_key": 64,
    "qdrant_read_key": 64,
    "qdrant_write_key": 64,
}
STORAGE_KEYS = ("s3_access_key", "s3_secret_key")
PROFILES: dict[str, tuple[str, ...]] = {
    "operator": tuple(SECRET_LENGTHS),
    "database-init": (
        "postgres_password",
        *(f"{role}_password" for role in DATABASE_ROLES),
        "keycloak_password",
    ),
    "postgres": ("postgres_password",),
    "identity": ("keycloak_password", "keycloak_admin_password"),
    "api": (
        "ed_app_password",
        "ed_identity_password",
        "oidc_client_secret",
        "session_key",
        "cursor_key",
        *STORAGE_KEYS,
    ),
    "ingest": ("ed_app_password", "ed_dispatch_password", *STORAGE_KEYS),
    "index": ("ed_app_password", "ed_dispatch_password", "qdrant_write_key", *STORAGE_KEYS),
    "chat": (
        "ed_app_password",
        "ed_identity_password",
        "ed_dispatch_password",
        "ed_checkpoint_password",
        "qdrant_read_key",
    ),
    "brief": ("ed_app_password", "ed_dispatch_password", "ed_checkpoint_password"),
    "probe": ("ed_app_password", "ed_dispatch_password"),
    "metrics": ("ed_monitor_password",),
    "qdrant": ("qdrant_read_key", "qdrant_write_key"),
    "storage": ("s3_admin_access_key", "s3_admin_secret_key", *STORAGE_KEYS),
    "storage-init": ("s3_admin_access_key", "s3_admin_secret_key"),
}


def private_directory(path: Path, *, uid: int = RUNTIME_UID, gid: int = RUNTIME_GID) -> None:
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    if not stat.S_ISDIR(path.lstat().st_mode):
        raise ValueError("Secret directory must be a real directory")
    os.chmod(path, 0o700, follow_symlinks=False)
    os.chown(path, uid, gid, follow_symlinks=False)


def read_private(path: Path) -> bytes:
    """Reject linked, shared, nonregular or oversized credential files."""
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as handle:
        info = os.fstat(handle.fileno())
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_nlink != 1
            or info.st_mode & 0o077
            or info.st_size > 1024 * 1024
        ):
            raise ValueError("Credential file must be private, regular and bounded")
        return handle.read()


def write_private(
    path: Path,
    data: bytes,
    *,
    uid: int = RUNTIME_UID,
    gid: int = RUNTIME_GID,
    replace: bool = False,
) -> bool:
    """Atomically persist a private file, rejecting unrequested credential rotation."""
    if len(data) > 1024 * 1024:
        raise ValueError("Bootstrap file exceeds the size limit")
    private_directory(path.parent, uid=uid, gid=gid)
    if path.exists() or path.is_symlink():
        if read_private(path) == data:
            os.chown(path, uid, gid, follow_symlinks=False)
            return False
        if not replace:
            raise ValueError("Existing bootstrap file differs; automatic rotation is forbidden")
    fd, temporary = tempfile.mkstemp(prefix=".init-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            os.fchmod(handle.fileno(), 0o600)
            os.fchown(handle.fileno(), uid, gid)
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        parent = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fsync(parent)
        finally:
            os.close(parent)
        return True
    finally:
        Path(temporary).unlink(missing_ok=True)


def initialize(
    root: Path,
    *,
    uid: int = RUNTIME_UID,
    gid: int = RUNTIME_GID,
    profile_owners: dict[str, tuple[int, int]] | None = None,
) -> None:
    owners = PROFILE_OWNERS if profile_owners is None else profile_owners
    private_directory(root, uid=uid, gid=gid)
    lock = os.open(root / ".bootstrap.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(lock, fcntl.LOCK_EX)
        canonical = root / "operator"
        private_directory(canonical, uid=uid, gid=gid)
        values: dict[str, bytes] = {}
        for name, length in SECRET_LENGTHS.items():
            path = canonical / name
            if path.exists() or path.is_symlink():
                value = read_private(path)
                if not re.fullmatch(rb"[a-f0-9]{" + str(length).encode() + rb"}\n", value):
                    raise ValueError("Persisted credential has an unexpected format")
            else:
                value = (secrets.token_hex(length // 2) + "\n").encode()
            write_private(path, value, uid=uid, gid=gid)
            values[name] = value
        for profile, names in PROFILES.items():
            profile_uid, profile_gid = owners.get(profile, (uid, gid))
            private_directory(root / profile, uid=profile_uid, gid=profile_gid)
            for name in names:
                write_private(root / profile / name, values[name], uid=profile_uid, gid=profile_gid)
    finally:
        os.close(lock)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path("/secrets"))
    parser.add_argument("--uid", type=int, default=RUNTIME_UID)
    parser.add_argument("--gid", type=int, default=RUNTIME_GID)
    args = parser.parse_args()
    try:
        initialize(args.root, uid=args.uid, gid=args.gid)
    except Exception:
        raise SystemExit(
            "Credential initialization failed; check private volume ownership/state."
        ) from None
    print("Persistent credentials initialized; existing values retained.")


if __name__ == "__main__":
    main()
