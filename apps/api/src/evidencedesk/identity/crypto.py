import base64
import hashlib
import hmac
import json
import re
from typing import Any

from cryptography.fernet import Fernet

from evidencedesk.errors import Problem


def digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def csrf_token(secret: bytes, session: str) -> str:
    return hmac.new(secret, b"csrf:" + session.encode(), hashlib.sha256).hexdigest()


def state_cipher(secret: bytes) -> Fernet:
    key = hmac.new(secret, b"login-state-encryption-v1", hashlib.sha256).digest()
    return Fernet(base64.urlsafe_b64encode(key))


def return_path(value: str) -> str:
    # Exact path allowlist: percent-encoding, controls, backslashes and dot traversal are rejected.
    if value != "/" and not re.fullmatch(r"/workspaces/[a-zA-Z0-9_/-]{1,180}", value):
        raise Problem(422, "invalid_return_path", "Choose a workspace path.")
    if "//" in value:
        raise Problem(422, "invalid_return_path", "Choose a workspace path.")
    return value


def canonical_json(value: Any) -> str:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    )
