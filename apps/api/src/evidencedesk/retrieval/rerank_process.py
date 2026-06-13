"""Prewarmed, single-flight inference child with kill/reap on timeout or cancellation."""

import asyncio
import json
import re
import sys
from pathlib import Path

from evidencedesk.retrieval.rerank_protocol import (
    MAX_REQUEST_BYTES,
    MAX_RESPONSE_BYTES,
    ScoreRequest,
    ScoreResponse,
)


class RerankUnavailable(Exception):
    pass


class RerankProcess:
    def __init__(self, cache: Path, lock: Path, *, deadline: float = 2):
        if not 0 < deadline <= 2:
            raise ValueError("Reranking deadline must be at most two seconds")
        self.cache, self.lock, self.deadline = cache.resolve(), lock.resolve(), deadline
        self.process: asyncio.subprocess.Process | None = None
        self.fingerprint: str | None = None
        self.busy = False

    async def start(self) -> None:
        # Lifecycle only: no lazy model load or replay inside score().
        if self.busy:
            raise RerankUnavailable()
        self.busy = True
        try:
            await self.close()
            self.process = await asyncio.create_subprocess_exec(
                sys.executable,
                "-I",
                "-m",
                "evidencedesk.retrieval.rerank_entry",
                str(self.cache),
                str(self.lock),
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
                limit=MAX_RESPONSE_BYTES,
                env={
                    "HF_HUB_OFFLINE": "1",
                    "TRANSFORMERS_OFFLINE": "1",
                    "TOKENIZERS_PARALLELISM": "false",
                    "OMP_NUM_THREADS": "4",
                    "MKL_NUM_THREADS": "4",
                    "PYTHONUNBUFFERED": "1",
                },
            )
            assert self.process.stdout is not None
            async with asyncio.timeout(45):
                ready = json.loads(await self.process.stdout.readline())
                fingerprint = ready["ready"]
                if not isinstance(fingerprint, str) or not re.fullmatch(
                    r"[a-f0-9]{64}", fingerprint
                ):
                    raise ValueError("Invalid readiness response")
                self.fingerprint = fingerprint
        except BaseException:
            await self.close()
            raise
        finally:
            self.busy = False

    async def close(self) -> None:
        process, self.process = self.process, None
        self.fingerprint = None
        if process is None:
            return
        if process.returncode is None:
            try:
                process.kill()
            except ProcessLookupError:
                pass
        # A repeated cancellation must not release the CPU slot while this child lives.
        task = asyncio.create_task(process.wait())
        cancelled = False
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                cancelled = True
        task.result()
        if cancelled:
            raise asyncio.CancelledError()

    async def score(self, request: ScoreRequest) -> ScoreResponse:
        process = self.process
        if (
            self.busy
            or process is None
            or process.returncode is not None
            or self.fingerprint is None
        ):
            raise RerankUnavailable()
        self.busy = True
        try:
            assert process.stdin is not None and process.stdout is not None
            async with asyncio.timeout(self.deadline):
                value = request.model_dump_json().encode() + b"\n"
                if len(value) > MAX_REQUEST_BYTES:
                    raise ValueError("Scoring request too large")
                process.stdin.write(value)
                await process.stdin.drain()
                response = ScoreResponse.model_validate_json(await process.stdout.readline())
                if response.fingerprint != self.fingerprint or [s.id for s in response.scores] != [
                    p.id for p in request.passages
                ]:
                    raise ValueError("Inconsistent scoring output")
                return response
        except asyncio.CancelledError:
            await self.close()
            raise
        except TimeoutError, ValueError, OSError:
            await self.close()
            raise RerankUnavailable() from None
        finally:
            self.busy = False
