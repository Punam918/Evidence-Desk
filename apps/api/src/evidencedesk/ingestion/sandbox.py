"""One bounded, no-network container per extraction. Docker is an operator-only worker capability."""

import asyncio
import json
import re
from contextlib import suppress
from pathlib import Path
from typing import Protocol
from uuid import uuid4

from evidencedesk.errors import Problem
from evidencedesk.ingestion.blocks import BlocksManifest
from pydantic import ValidationError

SAFE_ERRORS = {
    "extraction_limit",
    "block_limit",
    "empty_document",
    "encrypted_pdf",
    "page_limit",
    "ocr_required",
    "partial_coverage",
    "table_limit",
    "json_depth",
    "duplicate_json_key",
    "invalid_json",
    "json_entries",
    "parse_failed",
    "object_checksum_mismatch",
    "manifest_limit",
    "unsupported_file_type",
    "upload_size",
    "mime_mismatch",
    "unsupported_encoding",
    "invalid_archive",
    "archive_limits",
    "unsafe_archive",
    "invalid_docx",
}


class Parser(Protocol):
    image: str

    async def parse(
        self, source: Path, title: str, mime: str, sha256: str, output: Path
    ) -> BlocksManifest: ...


class ParserSandbox:
    def __init__(self, image: str, *, seconds: float = 60):
        if not re.fullmatch(r"sha256:[a-f0-9]{64}", image):
            raise ValueError("Parser requires a locally built immutable image ID")
        self.image, self.seconds = image, seconds

    async def parse(
        self, source: Path, title: str, mime: str, sha256: str, output: Path
    ) -> BlocksManifest:
        # source is a worker-created scratch file, never a user-provided path.
        name = "ed-parser-" + uuid4().hex
        process: asyncio.subprocess.Process | None = None
        try:
            async with asyncio.timeout(self.seconds):
                process = await asyncio.create_subprocess_exec(
                    "docker",
                    "run",
                    "--rm",
                    "--name",
                    name,
                    "--pull=never",
                    "--network=none",
                    "--read-only",
                    "--user=65532:65532",
                    "--cap-drop=ALL",
                    "--security-opt=no-new-privileges",
                    "--pids-limit=32",
                    "--memory=1g",
                    "--memory-swap=1g",
                    "--cpus=1",
                    "--ulimit",
                    "nofile=64:64",
                    "--tmpfs",
                    "/tmp:rw,noexec,nosuid,nodev,size=134217728,mode=1777",
                    "--mount",
                    f"type=bind,source={source.resolve()},target=/input/source,readonly",
                    "--log-driver=none",
                    "-i",
                    self.image,
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.DEVNULL,
                )
                assert process.stdin and process.stdout
                process.stdin.write(
                    json.dumps({"title": title, "mime": mime, "sha256": sha256}).encode()
                )
                await process.stdin.drain()
                process.stdin.close()
                count = 0
                with output.open("wb") as target:
                    while chunk := await process.stdout.read(65536):
                        count += len(chunk)
                        if count > 256 * 1024 * 1024:
                            raise Problem(
                                422, "manifest_limit", "Parser output exceeded its size limit."
                            )
                        target.write(chunk)
                code = await process.wait()
                if code == 2 and output.stat().st_size <= 1024:
                    error = json.loads(output.read_text()).get("error_code")
                    if error in SAFE_ERRORS:
                        raise Problem(
                            422, error, "Document extraction failed its format or coverage checks."
                        )
                if code != 0:
                    raise Problem(
                        422,
                        "parser_resource_failure",
                        "Parser failed or exceeded its resource limit.",
                    )
                try:
                    result = await asyncio.to_thread(
                        BlocksManifest.model_validate_json, output.read_bytes()
                    )
                except (ValueError, ValidationError) as exc:
                    raise Problem(
                        422, "invalid_manifest", "Parser output did not pass validation."
                    ) from exc
                if result.original_sha256 != sha256 or result.mime_type != mime:
                    raise Problem(
                        422, "invalid_manifest", "Parser output identity did not match its input."
                    )
                return result
        except TimeoutError as exc:
            raise Problem(
                422, "parser_timeout", "Document extraction exceeded 60 seconds."
            ) from exc
        finally:
            # Removing by a unique owned name is safe after timeout, cancellation or a normal exit.
            cleanup = await asyncio.create_subprocess_exec(
                "docker",
                "rm",
                "-f",
                name,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            try:
                await asyncio.wait_for(cleanup.wait(), 10)
            except TimeoutError:
                cleanup.kill()
                await cleanup.wait()
            if process and process.returncode is None:
                with suppress(ProcessLookupError):
                    process.kill()
                await process.wait()
