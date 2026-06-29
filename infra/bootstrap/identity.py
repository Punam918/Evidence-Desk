"""Generate a local realm import; never overwrite an existing identity configuration."""

import json
from pathlib import Path
from urllib.parse import urlsplit

from infra.bootstrap.secrets import read_private, write_private

PUBLIC_ORIGIN = "https://evidencedesk.localhost:8443"


def configure(root: Path, origin: str = PUBLIC_ORIGIN) -> None:
    if origin != PUBLIC_ORIGIN:
        raise ValueError("Local Compose identity requires its fixed HTTPS origin")
    operator = root / "operator"
    target = root / "identity"
    target.mkdir(mode=0o700, exist_ok=True)
    realm = {
        "realm": "evidencedesk",
        "enabled": True,
        "sslRequired": "all",
        "registrationAllowed": False,
        "resetPasswordAllowed": False,
        "loginWithEmailAllowed": True,
        "bruteForceProtected": True,
        "eventsEnabled": False,
        "adminEventsEnabled": False,
        "clients": [
            {
                "clientId": "evidencedesk",
                "protocol": "openid-connect",
                "enabled": True,
                "publicClient": False,
                "secret": read_private(operator / "oidc_client_secret").decode().strip(),
                "standardFlowEnabled": True,
                "implicitFlowEnabled": False,
                "directAccessGrantsEnabled": False,
                "serviceAccountsEnabled": False,
                "redirectUris": [origin + "/auth/callback"],
                "webOrigins": [origin],
                "attributes": {"pkce.code.challenge.method": "S256"},
                "defaultClientScopes": ["web-origins", "acr", "profile", "email"],
            }
        ],
        "users": [
            {
                "username": "demo",
                "enabled": True,
                "email": "demo@evidencedesk.example",
                "emailVerified": True,
                "firstName": "Demo",
                "lastName": "User",
                "credentials": [
                    {
                        "type": "password",
                        "value": read_private(operator / "demo_password").decode().strip(),
                        "temporary": False,
                    }
                ],
            }
        ],
    }
    write_private(target / "evidencedesk-realm.json", json.dumps(realm).encode())
    # Values are generated hex; no user-controlled property/newline interpolation.
    password = read_private(operator / "keycloak_password").decode().strip()
    if not password or any(c not in "0123456789abcdef" for c in password):
        raise ValueError("Unexpected database credential encoding")
    config = (
        "db=postgres\ndb-url=jdbc:postgresql://postgres:5432/keycloak\n"
        "db-username=ed_keycloak\ndb-password=" + password + "\n"
        "hostname=" + origin + "/identity\nhttp-enabled=true\nhttp-relative-path=/identity\n"
        "proxy-headers=xforwarded\nhealth-enabled=true\n"
        "http-management-relative-path=/\ncache=local\n"
    )
    assert urlsplit(origin).scheme == "https"
    # Generated server properties follow the image; canonical credentials are retained.
    write_private(target / "keycloak.conf", config.encode(), replace=True)
