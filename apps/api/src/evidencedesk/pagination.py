"""Authenticated keyset cursors bound to actor and the complete collection scope."""

import base64
import hashlib
import hmac
import json
from typing import Any

from evidencedesk.errors import Problem
from evidencedesk.identity.crypto import canonical_json


class Cursors:
    def __init__(self, key: bytes):
        self.key = key

    def encode(self, scope: dict[str, str], position: list[str]) -> str:
        data = canonical_json({"scope": scope, "position": position}).encode()
        mac = hmac.new(self.key, data, hashlib.sha256).digest()
        return base64.urlsafe_b64encode(mac + data).decode().rstrip("=")

    def decode(self, cursor: str | None, scope: dict[str, str]) -> list[str] | None:
        if cursor is None:
            return None
        try:
            if len(cursor) > 1024:
                raise ValueError("Too long")
            raw = base64.b64decode(cursor + "=" * (-len(cursor) % 4), altchars=b"-_", validate=True)
            mac, data = raw[:32], raw[32:]
            if not hmac.compare_digest(mac, hmac.new(self.key, data, hashlib.sha256).digest()):
                raise ValueError("Invalid signature")
            obj: dict[str, Any] = json.loads(data)
            if (
                obj["scope"] != scope
                or not isinstance(obj["position"], list)
                or not all(isinstance(x, str) for x in obj["position"])
            ):
                raise ValueError("Invalid scope")
            return list(obj["position"])
        except (ValueError, KeyError, TypeError) as exc:
            raise Problem(
                422, "invalid_cursor", "This page cursor is invalid for this collection."
            ) from exc
