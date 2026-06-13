"""Fixed child entry point. Never print user text, tracebacks or model output to logs."""

import contextlib
import json
import os
import sys
from pathlib import Path
from uuid import UUID

from evidencedesk.retrieval.rerank_protocol import MAX_REQUEST_BYTES, Pair, ScoreRequest


def main() -> int:
    output = sys.stdout
    # Model/library diagnostics are not part of the protocol and can contain input data.
    with (
        open(os.devnull, "w") as sink,
        contextlib.redirect_stdout(sink),
        contextlib.redirect_stderr(sink),
    ):
        from evidencedesk.retrieval.rerank_model import MiniLM

        model = MiniLM(Path(sys.argv[1]), Path(sys.argv[2]))
        model.score(ScoreRequest(query="warmup", passages=[Pair(id=UUID(int=1), text="warmup")]))
        output.write(json.dumps({"ready": model.fingerprint}) + "\n")
        output.flush()
        while line := sys.stdin.buffer.readline(MAX_REQUEST_BYTES + 1):
            if len(line) > MAX_REQUEST_BYTES or not line.endswith(b"\n"):
                return 2
            request = ScoreRequest.model_validate_json(line)
            response = model.score(request)
            output.write(response.model_dump_json() + "\n")
            output.flush()
            del request, response, line
    return 0


if __name__ == "__main__":
    try:
        code = main()
    except Exception:
        code = 2
    sys.exit(code)
