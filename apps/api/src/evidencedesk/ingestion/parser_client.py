"""Ingestion-only client for the networkless Compose parser service."""

import asyncio
import json
import struct
from contextlib import suppress
from pathlib import Path

from evidencedesk.errors import Problem
from evidencedesk.ingestion.blocks import BlocksManifest
from evidencedesk.ingestion.parser_protocol import (
    HEADER_LIMIT,
    MANIFEST_LIMIT,
    PROTOCOL_VERSION,
    envelope,
    service_fingerprint,
    validate_request,
)
from evidencedesk.ingestion.sandbox import SAFE_ERRORS


class SocketParserSandbox:
    def __init__(self, socket_path: Path, lock_path: Path, *, seconds: float = 100):
        self.fingerprint = service_fingerprint(lock_path)
        self.image = "source-sha256:" + self.fingerprint
        self.socket_path, self.seconds = socket_path, seconds

    async def parse(
        self, source: Path, title: str, mime: str, sha256: str, output: Path
    ) -> BlocksManifest:
        writer: asyncio.StreamWriter | None = None
        try:
            async with asyncio.timeout(self.seconds):
                request = {
                    "version": PROTOCOL_VERSION,
                    "operation": "parse",
                    "fingerprint": self.fingerprint,
                    "title": title,
                    "mime": mime,
                    "sha256": sha256,
                    "size": source.stat().st_size,
                }
                validate_request(request, self.fingerprint)
                raw = json.dumps(request, separators=(",", ":")).encode()
                if len(raw) > HEADER_LIMIT:
                    raise ValueError("Parser metadata too large")
                reader, writer = await asyncio.open_unix_connection(self.socket_path)
                writer.write(struct.pack("!I", len(raw)) + raw)
                with source.open("rb") as content:
                    while chunk := content.read(65536):
                        writer.write(chunk)
                        await writer.drain()
                size = struct.unpack("!I", await reader.readexactly(4))[0]
                if not 0 < size <= HEADER_LIMIT:
                    raise ValueError("Invalid parser response size")
                response = envelope(await reader.readexactly(size))
                expected_fields = {"version", "fingerprint", "status", "size"}
                if response.get("status") == "error":
                    expected_fields.add("error_code")
                if (
                    set(response) != expected_fields
                    or type(response["version"]) is not int
                    or response["version"] != PROTOCOL_VERSION
                    or response["fingerprint"] != self.fingerprint
                    or type(response["size"]) is not int
                ):
                    raise ValueError("Parser response identity does not match")
                if response["status"] == "error":
                    code = response["error_code"]
                    if response["size"] != 0 or not isinstance(code, str):
                        raise ValueError("Invalid parser error")
                    if code in SAFE_ERRORS | {"parser_timeout", "parser_resource_failure"}:
                        raise Problem(
                            422, code, "Document extraction failed its format or resource checks."
                        )
                    raise ValueError("Parser service failed its boundary checks")
                if response["status"] != "ok" or not 0 < response["size"] <= MANIFEST_LIMIT:
                    raise ValueError("Invalid parser manifest size")
                remaining = response["size"]
                with output.open("wb") as target:
                    while remaining:
                        chunk = await reader.readexactly(min(remaining, 65536))
                        target.write(chunk)
                        remaining -= len(chunk)
                if await reader.read(1):
                    raise ValueError("Trailing parser response bytes")
                result = await asyncio.to_thread(
                    BlocksManifest.model_validate_json, output.read_bytes()
                )
                if result.original_sha256 != sha256 or result.mime_type != mime:
                    raise ValueError("Parser manifest identity does not match")
                return result
        except TimeoutError as exc:
            raise Problem(
                503,
                "parser_service_timeout",
                "The isolated parser did not respond in time.",
                retryable=True,
            ) from exc
        except (OSError, ValueError, asyncio.IncompleteReadError) as exc:
            raise Problem(
                503,
                "parser_service_unavailable",
                "The isolated parser is unavailable or failed verification.",
                retryable=True,
            ) from exc
        finally:
            if writer:
                writer.close()
                with suppress(OSError, TimeoutError):
                    await asyncio.wait_for(writer.wait_closed(), 2)
