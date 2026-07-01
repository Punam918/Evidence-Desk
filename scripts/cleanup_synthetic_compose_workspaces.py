"""Remove only named synthetic Compose smoke workspaces through the public API."""

import socket
from unittest.mock import patch
from uuid import uuid4

import httpx

from validate_compose import ORIGIN, login, mounted_file

PREFIX = "Harbor operations · integration "


def main() -> None:
    ca = mounted_file("evidencedesk", "trust/ca.crt")
    password = mounted_file("evidencedesk", "operator/demo_password").decode().strip()
    context = __import__("ssl").create_default_context(cadata=ca.decode())
    resolver = socket.getaddrinfo

    def local_dns(host: str, *args: object, **kwargs: object) -> object:
        return resolver("127.0.0.1" if host == "evidencedesk.localhost" else host, *args, **kwargs)

    with patch("socket.getaddrinfo", local_dns), httpx.Client(
        base_url=ORIGIN, verify=context, trust_env=False, timeout=15, follow_redirects=False
    ) as client:
        identity = login(client, password)
        headers = {"origin": ORIGIN, "x-csrf-token": identity["csrf_token"]}
        response = client.get("/v1/workspaces?limit=100")
        response.raise_for_status()
        deleted = 0
        for workspace in response.json()["items"]:
            if not workspace["name"].startswith(PREFIX):
                continue
            result = client.delete(
                "/v1/workspaces/" + workspace["id"],
                headers={**headers, "idempotency-key": str(uuid4())},
            )
            result.raise_for_status()
            deleted += 1
    print(f"Removed {deleted} synthetic Compose smoke workspaces.")


if __name__ == "__main__":
    main()
