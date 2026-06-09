import base64
import hashlib
import hmac
import json
import secrets
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from cryptography.fernet import InvalidToken
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine

from evidencedesk.config import Settings
from evidencedesk.db.identity_repository import IdentityRepository
from evidencedesk.db.session import AuthContext
from evidencedesk.errors import Problem
from evidencedesk.identity.crypto import csrf_token, digest, return_path, state_cipher
from evidencedesk.identity.oidc import OIDC


@dataclass(frozen=True)
class Identity:
    user_id: UUID
    email: str
    display_name: str
    expires_at: datetime
    session_hash: str
    csrf: str


class IdentityService:
    def __init__(self, settings: Settings, db: AsyncEngine, oidc: OIDC):
        self.settings, self.oidc = settings, oidc
        self.repository = IdentityRepository(db)
        self.secret = settings.session_key()
        self.cipher = state_cipher(self.secret)

    async def login(self, target: str) -> tuple[str, str]:
        path = return_path(target)
        state, nonce, verifier, browser = (secrets.token_urlsafe(32) for _ in range(4))
        ciphertext = self.cipher.encrypt(
            json.dumps({"verifier": verifier, "browser_hash": digest(browser)}).encode()
        )
        await self.repository.start_login(digest(state), digest(nonce), ciphertext, path)
        challenge = (
            base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
            .rstrip(b"=")
            .decode()
        )
        return self.oidc.authorization_url(state, nonce, challenge), browser

    async def callback(
        self, code: str, state: str, browser: str | None, request_id: UUID
    ) -> tuple[str, str]:
        if not browser or len(browser) > 128:
            raise Problem(401, "invalid_login_state", "Restart sign in in this browser.")
        row = await self.repository.consume_login(digest(state))
        if not row:
            raise Problem(401, "invalid_login_state", "Restart sign in in this browser.")
        try:
            payload = json.loads(self.cipher.decrypt(row["cipher"], ttl=300))
            if not hmac.compare_digest(payload["browser_hash"], digest(browser)):
                raise ValueError("Browser mismatch")
        except (InvalidToken, ValueError, KeyError) as exc:
            raise Problem(401, "invalid_login_state", "Restart sign in in this browser.") from exc
        claims = await self.oidc.exchange(code, payload["verifier"], row["nonce_hash"])
        session = secrets.token_urlsafe(32)
        try:
            await self.repository.finish_login(
                claims["iss"],
                claims["sub"],
                claims["email"].lower(),
                str(claims.get("name") or claims["email"])[:120],
                digest(session),
                digest(csrf_token(self.secret, session)),
                request_id,
            )
        except IntegrityError as exc:
            raise Problem(
                409, "identity_conflict", "This identity requires an administrator review."
            ) from exc
        return session, row["return_path"]

    async def authenticate(self, session: str | None) -> Identity:
        if not session or len(session) > 128:
            raise Problem(401, "authentication_required", "Sign in to continue.")
        token = csrf_token(self.secret, session)
        row = await self.repository.session(digest(session))
        if not row or not hmac.compare_digest(row["csrf_hash"], digest(token)):
            raise Problem(401, "authentication_required", "Sign in to continue.")
        return Identity(
            row["user_id"],
            row["email"],
            row["display_name"],
            row["expires_at"],
            digest(session),
            token,
        )

    def mutation(self, identity: Identity, origin: str | None, csrf: str | None) -> None:
        if (
            origin != self.settings.public_origin
            or not csrf
            or not hmac.compare_digest(identity.csrf, csrf)
        ):
            raise Problem(403, "csrf_failed", "Refresh the page and try again.")

    async def logout(self, identity: Identity, key: str, request_id: UUID) -> None:
        await self.repository.logout(identity.session_hash, digest(key), digest("{}"), request_id)

    async def rate(self, identity: Identity, mutation: bool) -> None:
        allowed = await self.repository.consume_rate(identity.user_id, "request", 120)
        if mutation:
            allowed = (
                await self.repository.consume_rate(identity.user_id, "mutation", 30) and allowed
            )
        if not allowed:
            raise Problem(429, "rate_limited", "Too many requests. Try again next minute.", True)

    async def context(
        self, identity: Identity, workspace: UUID, request_id: UUID, minimum: str = "viewer"
    ) -> AuthContext:
        row = await self.repository.membership(identity.user_id, workspace)
        if not row:
            raise Problem(404, "not_found", "Workspace not found.")
        roles = ["viewer", "editor", "admin", "owner"]
        if roles.index(row["role"]) < roles.index(minimum):
            raise Problem(403, "forbidden", "Your role does not allow this action.")
        return AuthContext(identity.user_id, workspace, row["role"], row["version"], request_id)
