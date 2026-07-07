import asyncio
import hashlib
import json
import struct
from pathlib import Path

import pytest
from evidencedesk.errors import Problem
from evidencedesk.ingestion import parser_client
from evidencedesk.ingestion.parser_protocol import envelope, validate_request
from evidencedesk.ingestion.parsers.core import extract

FINGERPRINT = "e" * 64


@pytest.mark.parametrize("raw", [b"[]", b'{"a":1,"a":2}', b"{" * 4097, b"[" * 1500 + b"]" * 1500])
def test_untrusted_envelope_rejected_without_unbounded_recursion(raw):
    with pytest.raises(ValueError):
        envelope(raw)


@pytest.mark.parametrize(
    "change",
    [
        {"size": True},
        {"size": 21 * 1024**2},
        {"version": True},
        {"fingerprint": "x" * 64},
        {"sha256": "z" * 64},
        {"title": "x\n.txt"},
        {"path": "/etc/passwd"},
    ],
)
def test_request_metadata_cannot_select_path_command_or_implementation(change):
    request = {
        "version": 1,
        "operation": "parse",
        "fingerprint": FINGERPRINT,
        "title": "doc.txt",
        "mime": "text/plain",
        "sha256": "1" * 64,
        "size": 10,
    }
    with pytest.raises(ValueError):
        validate_request({**request, **change}, FINGERPRINT)


@pytest.mark.parametrize(
    "failure",
    [
        "wrong_fingerprint",
        "wrong_source",
        "oversized_header",
        "truncated_body",
        "trailing_body",
        "boolean_version",
        "unknown_error",
        "deadline",
    ],
)
async def test_client_fails_closed_on_unverified_service_output(tmp_path, monkeypatch, failure):
    monkeypatch.setattr(parser_client, "service_fingerprint", lambda _: FINGERPRINT)
    source = tmp_path / "source"
    source.write_text("Trusted fixture text.")
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    manifest = extract(source, "doc.txt", "text/plain", digest)
    if failure == "wrong_source":
        manifest.original_sha256 = "1" * 64
    body = manifest.model_dump_json().encode()
    socket_path = tmp_path / "parser.sock"

    async def respond(reader, writer):
        try:
            size = struct.unpack("!I", await reader.readexactly(4))[0]
            request = json.loads(await reader.readexactly(size))
            await reader.readexactly(request["size"])
            if failure == "deadline":
                await reader.read()
                return
            response = {"version": 1, "fingerprint": FINGERPRINT, "status": "ok", "size": len(body)}
            if failure == "wrong_fingerprint":
                response["fingerprint"] = "a" * 64
            if failure == "boolean_version":
                response["version"] = True
            if failure == "unknown_error":
                response.update(
                    {"status": "error", "size": 0, "error_code": "raw_private_exception"}
                )
            header = json.dumps(response).encode()
            writer.write(struct.pack("!I", 4097 if failure == "oversized_header" else len(header)))
            writer.write(header)
            writer.write(body[:-1] if failure == "truncated_body" else body)
            if failure == "trailing_body":
                writer.write(b"x")
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()

    server = await asyncio.start_unix_server(respond, socket_path)
    try:
        with pytest.raises(Problem) as exc:
            await parser_client.SocketParserSandbox(socket_path, Path("unused"), seconds=0.2).parse(
                source, "doc.txt", "text/plain", digest, tmp_path / "out"
            )
        assert exc.value.code == (
            "parser_service_timeout" if failure == "deadline" else "parser_service_unavailable"
        )
        assert exc.value.retryable
    finally:
        server.close()
        await server.wait_closed()
