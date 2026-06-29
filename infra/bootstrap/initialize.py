"""Initialize only local Compose-owned volumes, before any serving process starts."""

import os
from pathlib import Path

from infra.bootstrap import identity, secrets, storage, tls


def main() -> None:
    try:
        root = Path("/secrets")
        secrets.initialize(root)
        identity.configure(root)
        storage.configure(root)
        tls.configure(root)
        # Compose named volumes start root-owned. Only this one-shot job receives
        # these mounts writable; do not recursively chown existing application data.
        for name in (
            "models",
            "identity-data",
            "objects",
            "qdrant-data",
            "qdrant-snapshots",
            "caddy-data",
            "caddy-config",
        ):
            path = Path("/volumes") / name
            path.mkdir(exist_ok=True)
            os.chown(path, 10001, 10001)
            os.chmod(path, 0o700)
        print("Local configuration initialized; persisted credentials and CA retained.")
    except Exception:
        raise SystemExit(
            "Local bootstrap failed; check private volume state and ownership."
        ) from None


if __name__ == "__main__":
    main()
