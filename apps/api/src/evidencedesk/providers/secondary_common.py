"""Shared conservative accounting and sanitized failures for secondary native transports."""

import json
import math
from typing import Any

from evidencedesk.providers.base import ModelFault, TokenCount


def estimate_body(body: dict[str, Any]) -> TokenCount:
    # No unverified foreign tokenizer is treated as exact. A native count still requires egress authorization.
    size = len(json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode())
    return TokenCount(
        math.ceil(size * 1.2) + 64, True, "utf8_full_json_upper_bound_plus_20_percent"
    )


def http_fault(status: int, retry_after: str | None = None) -> ModelFault:
    delay = None
    if retry_after:
        try:
            value = float(retry_after)
            if math.isfinite(value) and value >= 0:
                delay = value
        except ValueError:
            pass
    definite = status in (400, 401, 403, 404, 422, 429)
    return ModelFault(
        "provider_rate_limited"
        if status == 429
        else "provider_configuration"
        if definite
        else "provider_unavailable",
        "definite_failure" if definite else "ambiguous",
        status == 429,
        delay,
    )
