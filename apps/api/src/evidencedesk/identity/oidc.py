"""Fixed-issuer OIDC code exchange and asymmetric signature/claim validation."""

import hashlib
import hmac
from typing import Any
from urllib.parse import urlencode, urlsplit

import httpx
from joserfc import jwk, jwt
from joserfc.errors import JoseError

from evidencedesk.config import Settings
from evidencedesk.errors import Problem


class OIDC:
    def __init__(self, settings: Settings, client: httpx.AsyncClient):
        self.settings = settings
        self.client = client
        self.metadata: dict[str, Any] = {}

    async def discover(self) -> None:
        response = await self.client.get(
            self.settings.oidc_issuer + "/.well-known/openid-configuration"
        )
        response.raise_for_status()
        metadata = response.json()
        if metadata.get("issuer") != self.settings.oidc_issuer:
            raise ValueError("OIDC discovery issuer mismatch")
        origin = urlsplit(self.settings.oidc_issuer)
        for key in ["authorization_endpoint", "token_endpoint", "jwks_uri"]:
            endpoint = urlsplit(metadata[key])
            # This deployment requires co-located IdP endpoints; no dynamic redirect or token URL.
            if (
                (endpoint.scheme, endpoint.netloc) != (origin.scheme, origin.netloc)
                or endpoint.username
                or endpoint.fragment
            ):
                raise ValueError("OIDC endpoint outside configured issuer origin")
        self.metadata = metadata

    def authorization_url(self, state: str, nonce: str, challenge: str) -> str:
        return (
            str(self.metadata["authorization_endpoint"])
            + "?"
            + urlencode(
                {
                    "response_type": "code",
                    "client_id": self.settings.oidc_client_id,
                    "redirect_uri": self.settings.public_origin + "/auth/callback",
                    "scope": "openid email profile",
                    "state": state,
                    "nonce": nonce,
                    "code_challenge_method": "S256",
                    "code_challenge": challenge,
                }
            )
        )

    async def exchange(self, code: str, verifier: str, nonce_hash: str) -> dict[str, Any]:
        try:
            response = await self.client.post(
                self.metadata["token_endpoint"],
                data={
                    "grant_type": "authorization_code",
                    "code": code,
                    "code_verifier": verifier,
                    "redirect_uri": self.settings.public_origin + "/auth/callback",
                },
                auth=(
                    self.settings.oidc_client_id,
                    self.settings.oidc_client_secret_file.read_text().strip(),
                ),
            )
            response.raise_for_status()
            raw = response.json()["id_token"]
            if not isinstance(raw, str) or len(raw) > 16384:
                raise ValueError("Invalid ID token")
            keys = await self.client.get(self.metadata["jwks_uri"])
            keys.raise_for_status()
            token = jwt.decode(
                raw, jwk.KeySet.import_key_set(keys.json()), algorithms=["RS256", "ES256"]
            )
            claims = token.claims
            # Enforce types independently of registry matching (including array-valued issuer attacks).
            for field in ["iss", "sub", "nonce", "email"]:
                if not isinstance(claims.get(field), str) or not claims[field]:
                    raise ValueError("Invalid identity claim type")
            for field in ["exp", "iat"]:
                if type(claims.get(field)) not in (int, float):
                    raise ValueError("Missing timestamp")
            jwt.JWTClaimsRegistry(
                leeway=30,
                iss={"essential": True, "value": self.settings.oidc_issuer},
                aud={"essential": True, "value": self.settings.oidc_client_id},
                exp={"essential": True},
                iat={"essential": True},
                sub={"essential": True},
            ).validate(claims)
            audience = claims["aud"]
            if (
                isinstance(audience, list)
                and len(audience) > 1
                and claims.get("azp") != self.settings.oidc_client_id
            ):
                raise ValueError("Invalid authorized party")
            if "azp" in claims and claims["azp"] != self.settings.oidc_client_id:
                raise ValueError("Invalid authorized party")
            if not hmac.compare_digest(
                hashlib.sha256(claims["nonce"].encode()).hexdigest(), nonce_hash
            ):
                raise ValueError("Invalid nonce")
            if (
                claims.get("email_verified") is not True
                or "@" not in claims["email"]
                or len(claims["email"]) > 320
            ):
                raise ValueError("Verified email required")
            return dict(claims)
        except (JoseError, ValueError, KeyError, TypeError) as exc:
            raise Problem(401, "invalid_identity", "Identity verification failed.") from exc
        except httpx.HTTPError as exc:
            raise Problem(
                503, "identity_unavailable", "Identity provider is unavailable.", True
            ) from exc
