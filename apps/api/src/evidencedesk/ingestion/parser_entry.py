"""Unprivileged container entry point. Output is bounded JSON, never exception/source logs."""

import json
import logging
import sys
from pathlib import Path

from evidencedesk.errors import Problem
from evidencedesk.ingestion.parsers.core import extract


def main() -> None:
    logging.disable(logging.CRITICAL)
    try:
        raw = sys.stdin.buffer.read(4097)
        if len(raw) > 4096:
            raise ValueError("Oversized control envelope")
        request = json.loads(raw)
        if set(request) != {"title", "mime", "sha256"} or not all(
            isinstance(v, str) for v in request.values()
        ):
            raise ValueError("Invalid control envelope")
        result = extract(
            Path("/input/source"), request["title"], request["mime"], request["sha256"]
        )
        output = result.model_dump_json().encode()
        if len(output) > 256 * 1024 * 1024:
            raise Problem(422, "manifest_limit", "The extracted manifest exceeds its size limit.")
        sys.stdout.buffer.write(output)
    except Problem as exc:
        sys.stdout.write(json.dumps({"error_code": exc.code}))
        sys.exit(2)
    except Exception:
        sys.stdout.write('{"error_code":"parse_failed"}')
        sys.exit(2)


if __name__ == "__main__":
    main()
