"""One bounded extraction at a time, over a private Unix socket and no IP network."""

import hashlib
import json
import os
import select
import signal
import socket
import struct
import subprocess
import sys
import tempfile
import time
from contextlib import suppress
from pathlib import Path
from typing import Any

from evidencedesk.ingestion.parser_protocol import (
    HEADER_LIMIT,
    MANIFEST_LIMIT,
    PROTOCOL_VERSION,
    envelope,
    service_fingerprint,
    validate_request,
)
from evidencedesk.ingestion.sandbox import SAFE_ERRORS

SOCKET_PATH = Path("/run/parser/parser.sock")
LOCK_PATH = Path("/app/infra/parser/service.lock.json")


class Rejected(Exception):
    def __init__(self, code: str):
        self.code = code


def receive(connection: socket.socket, size: int, deadline: float) -> bytes:
    output = bytearray()
    while len(output) < size:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("Parser input deadline")
        connection.settimeout(remaining)
        chunk = connection.recv(min(65536, size - len(output)))
        if not chunk:
            raise ConnectionError("Parser client disconnected")
        output.extend(chunk)
    return bytes(output)


def respond(
    connection: socket.socket, fingerprint: str, *, size: int = 0, code: str | None = None
) -> None:
    response: dict[str, Any] = {
        "version": PROTOCOL_VERSION,
        "fingerprint": fingerprint,
        "status": "error" if code else "ok",
        "size": size,
    }
    if code:
        response["error_code"] = code
    raw = json.dumps(response, separators=(",", ":")).encode()
    connection.settimeout(10)
    connection.sendall(struct.pack("!I", len(raw)) + raw)


def extract_child(
    connection: socket.socket, source: Path, output: Path, request: dict[str, Any]
) -> None:
    with source.open("rb") as source_file, output.open("wb") as manifest:
        process = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "evidencedesk.ingestion.parser_service_child",
                str(source_file.fileno()),
            ],
            stdin=subprocess.PIPE,
            stdout=manifest,
            stderr=subprocess.DEVNULL,
            close_fds=True,
            pass_fds=(source_file.fileno(),),
            start_new_session=True,
            cwd="/",
            env={
                "PYTHONPATH": "/app/apps/api/src",
                "PYTHONDONTWRITEBYTECODE": "1",
                "PYTHONUNBUFFERED": "1",
                "LANG": "C.UTF-8",
            },
        )
        try:
            assert process.stdin
            process.stdin.write(
                json.dumps({key: request[key] for key in ("title", "mime", "sha256")}).encode()
            )
            process.stdin.close()
            deadline = time.monotonic() + 60
            while process.poll() is None:
                if time.monotonic() >= deadline:
                    raise Rejected("parser_timeout")
                # No bytes follow the exact source body. EOF or extra bytes cancel this child.
                ready, _, _ = select.select([connection], [], [], 0.05)
                if ready:
                    raise ConnectionError("Parser client disconnected or sent trailing data")
            if process.returncode == 3:
                raise Rejected("parser_isolation_failure")
            if process.returncode == 2 and output.stat().st_size <= 1024:
                with suppress(ValueError, KeyError):
                    code = envelope(output.read_bytes())["error_code"]
                    if code in SAFE_ERRORS:
                        raise Rejected(code)
            if process.returncode != 0:
                raise Rejected("parser_resource_failure")
            if not 0 < output.stat().st_size <= MANIFEST_LIMIT:
                raise Rejected("manifest_limit")
        finally:
            if process.poll() is None:
                # SETUID lets the supervisor signal its child without granting CAP_KILL.
                os.seteuid(65532)
                try:
                    with suppress(ProcessLookupError):
                        os.killpg(process.pid, signal.SIGKILL)
                finally:
                    os.seteuid(0)
            process.wait()


def handle(connection: socket.socket, fingerprint: str) -> None:
    deadline = time.monotonic() + 30
    try:
        header_size = struct.unpack("!I", receive(connection, 4, deadline))[0]
        if not 0 < header_size <= HEADER_LIMIT:
            raise ValueError("Parser header too large")
        request = envelope(receive(connection, header_size, deadline))
        if request == {
            "version": PROTOCOL_VERSION,
            "operation": "health",
            "fingerprint": fingerprint,
        }:
            respond(connection, fingerprint)
            return
        validate_request(request, fingerprint)
        # Only one active request; untrusted children cannot traverse root-only scratch.
        with tempfile.TemporaryDirectory(prefix="ed-parser-", dir="/tmp") as directory:
            scratch = Path(directory)
            source, output = scratch / "source", scratch / "manifest"
            digest = hashlib.sha256()
            remaining = request["size"]
            with source.open("wb") as target:
                while remaining:
                    chunk = receive(connection, min(remaining, 65536), deadline)
                    digest.update(chunk)
                    target.write(chunk)
                    remaining -= len(chunk)
            if digest.hexdigest() != request["sha256"]:
                raise Rejected("object_checksum_mismatch")
            source.chmod(0o444)  # Child can reopen its inherited fd through /proc/self/fd.
            extract_child(connection, source, output, request)
            respond(connection, fingerprint, size=output.stat().st_size)
            deadline = time.monotonic() + 10
            with output.open("rb") as manifest:
                while chunk := manifest.read(65536):
                    remaining_time = deadline - time.monotonic()
                    if remaining_time <= 0:
                        raise TimeoutError("Parser output deadline")
                    connection.settimeout(remaining_time)
                    connection.sendall(chunk)
    except Rejected as exc:
        respond(connection, fingerprint, code=exc.code)
    except ValueError, KeyError, TypeError:
        respond(connection, fingerprint, code="parser_protocol_failure")


def health(fingerprint: str) -> None:
    with socket.socket(socket.AF_UNIX) as connection:
        connection.settimeout(3)
        connection.connect(str(SOCKET_PATH))
        raw = json.dumps(
            {"version": PROTOCOL_VERSION, "operation": "health", "fingerprint": fingerprint}
        ).encode()
        connection.sendall(struct.pack("!I", len(raw)) + raw)
        deadline = time.monotonic() + 3
        size = struct.unpack("!I", receive(connection, 4, deadline))[0]
        response = envelope(receive(connection, min(size, HEADER_LIMIT + 1), deadline))
        if response != {
            "version": PROTOCOL_VERSION,
            "fingerprint": fingerprint,
            "status": "ok",
            "size": 0,
        }:
            raise ValueError("Parser health failed")


def main() -> None:
    fingerprint = service_fingerprint(LOCK_PATH, Path("/app"))
    if sys.argv[1:] == ["--health"]:
        health(fingerprint)
        return
    if sys.argv[1:] or os.getuid() != 0:
        raise SystemExit("Invalid parser supervisor configuration")
    os.umask(0o077)
    SOCKET_PATH.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
    # Shared volume root must remain root-owned: clients can connect, not replace the socket.
    info = SOCKET_PATH.parent.stat()
    if info.st_uid != 0 or info.st_mode & 0o022:
        raise SystemExit("Unsafe parser socket directory")
    SOCKET_PATH.parent.chmod(0o755)
    SOCKET_PATH.unlink(missing_ok=True)
    with socket.socket(socket.AF_UNIX) as server:
        os.setegid(10001)
        try:
            server.bind(str(SOCKET_PATH))
        finally:
            os.setegid(0)
        SOCKET_PATH.chmod(0o660)
        server.listen(1)
        print(json.dumps({"event": "parser_ready", "source_sha256": fingerprint}), flush=True)
        while True:
            connection, _ = server.accept()
            with connection:
                # Peer authentication is kernel-enforced within the shared Unix-socket mount.
                _, uid, _ = struct.unpack(
                    "3i", connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12)
                )
                if uid not in {0, 10001}:
                    continue
                with suppress(ConnectionError, OSError):
                    handle(connection, fingerprint)


if __name__ == "__main__":
    main()
