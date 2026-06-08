"""Only execute the identity role's explicitly granted functions."""

from datetime import datetime
from typing import TypedDict
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine


class LoginState(TypedDict):
    nonce_hash: str
    cipher: bytes
    return_path: str


class SessionRecord(TypedDict):
    user_id: UUID
    email: str
    display_name: str
    expires_at: datetime
    csrf_hash: str


class MembershipRecord(TypedDict):
    role: str
    version: int


class IdentityRepository:
    def __init__(self, db: AsyncEngine):
        self.db = db

    async def start_login(self, state: str, nonce: str, cipher: bytes, path: str) -> None:
        async with self.db.begin() as conn:
            await conn.execute(
                text("SELECT app.start_login(:state,:nonce,:cipher,:path)"),
                {"state": state, "nonce": nonce, "cipher": cipher, "path": path},
            )

    async def consume_login(self, state: str) -> LoginState | None:
        async with self.db.begin() as conn:
            row = (
                (
                    await conn.execute(
                        text("SELECT * FROM app.consume_login(:hash)"), {"hash": state}
                    )
                )
                .mappings()
                .first()
            )
            return (
                LoginState(
                    nonce_hash=row["nonce_hash"],
                    cipher=row["cipher"],
                    return_path=row["return_path"],
                )
                if row
                else None
            )

    async def finish_login(
        self,
        issuer: str,
        subject: str,
        email: str,
        name: str,
        session_hash: str,
        csrf_hash: str,
        request_id: UUID,
    ) -> None:
        async with self.db.begin() as conn:
            await conn.execute(
                text("SELECT app.finish_login(:issuer,:subject,:email,:name,:hash,:csrf,:request)"),
                {
                    "issuer": issuer,
                    "subject": subject,
                    "email": email,
                    "name": name,
                    "hash": session_hash,
                    "csrf": csrf_hash,
                    "request": request_id,
                },
            )

    async def session(self, session_hash: str) -> SessionRecord | None:
        async with self.db.begin() as conn:
            row = (
                (
                    await conn.execute(
                        text("SELECT * FROM app.lookup_session(:hash)"), {"hash": session_hash}
                    )
                )
                .mappings()
                .first()
            )
            return (
                SessionRecord(
                    user_id=row["user_id"],
                    email=row["email"],
                    display_name=row["display_name"],
                    expires_at=row["expires_at"],
                    csrf_hash=row["csrf_hash"],
                )
                if row
                else None
            )

    async def revoke(self, session_hash: str) -> None:
        async with self.db.begin() as conn:
            await conn.execute(text("SELECT app.revoke_session(:hash)"), {"hash": session_hash})

    async def membership(self, actor: UUID, workspace: UUID) -> MembershipRecord | None:
        async with self.db.begin() as conn:
            row = (
                (
                    await conn.execute(
                        text("SELECT * FROM app.get_membership(:actor,:workspace)"),
                        {"actor": actor, "workspace": workspace},
                    )
                )
                .mappings()
                .first()
            )
            return MembershipRecord(role=row["role"], version=row["version"]) if row else None

    async def consume_rate(self, actor: UUID, category: str, limit: int) -> bool:
        async with self.db.begin() as conn:
            return bool(
                (
                    await conn.execute(
                        text("SELECT app.consume_rate(:actor,:category,:limit)"),
                        {"actor": actor, "category": category, "limit": limit},
                    )
                ).scalar_one()
            )

    async def logout(
        self, session_hash: str, key_hash: str, body_hash: str, request_id: UUID
    ) -> None:
        async with self.db.begin() as conn:
            await conn.execute(
                text("SELECT app.logout_session(:session,:key,:body,:request)"),
                {
                    "session": session_hash,
                    "key": key_hash,
                    "body": body_hash,
                    "request": request_id,
                },
            )
