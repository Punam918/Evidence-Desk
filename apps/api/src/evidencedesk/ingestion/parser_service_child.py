"""Trusted tiny child bootstrap; drops privileges before importing format parsers."""

import json
import logging
import sys
from pathlib import Path

from evidencedesk.ingestion.parser_isolation import isolate


def main() -> None:
    logging.disable(logging.CRITICAL)
    try:
        source_fd = int(sys.argv[1])
        if source_fd < 3:
            raise ValueError("Invalid source descriptor")
        isolate()
    except Exception:
        sys.stdout.write('{"error_code":"parser_isolation_failure"}')
        sys.exit(3)
    # No parsing imports execute with supervisor privilege.
    from evidencedesk.errors import Problem
    from evidencedesk.ingestion.parser_protocol import MANIFEST_LIMIT, envelope
    from evidencedesk.ingestion.parsers.core import extract

    try:
        request = envelope(sys.stdin.buffer.read(4097))
        result = extract(
            Path(f"/proc/self/fd/{source_fd}"), request["title"], request["mime"], request["sha256"]
        )
        output = result.model_dump_json().encode()
        if len(output) > MANIFEST_LIMIT:
            raise Problem(422, "manifest_limit", "Parser output exceeded its size limit.")
        sys.stdout.buffer.write(output)
    except Problem as exc:
        sys.stdout.write(json.dumps({"error_code": exc.code}))
        sys.exit(2)
    except Exception:
        sys.stdout.write('{"error_code":"parse_failed"}')
        sys.exit(2)


if __name__ == "__main__":
    main()
