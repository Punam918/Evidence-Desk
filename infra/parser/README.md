# Isolated extraction image

Build from the repository root with `make parser-image`. The build script sends only the selected parser modules, Dockerfile and hash-locked requirements to Docker. It records the image identity and every build-input checksum in `image.lock.json`. Rebuild after changing parser inputs; integration tests reject a stale lock. This is a local build artifact, not a published registry image.

`ParserSandbox` starts one named container with an immutable local image ID, `--pull=never`, UID65532, read-only root filesystem, no network, dropped capabilities, no-new-privileges,32 processes,1CPU,1GiB RAM and no swap. Only the server-created input file is mounted read-only. Scratch is a128MiB noexec/nosuid/nodev tmpfs. The worker enforces a60s deadline, bounds output to256MiB, validates the JSON envelope and removes its owned container on success, failure or cancellation. No provider/database credentials or Docker socket enter the parser. Docker execution belongs to the trusted ingestion worker host; it must never be exposed to the API/browser or parser.

The image has only format-extraction dependencies. No inference framework, model weights, network downloader or arbitrary user-selected executable runs inside it. Dependency versions and wheel hashes come from the reviewed `uv.lock`. Format adapters enforce the limits in the ingestion design. The parser's text coverage excludes explicitly reported image and format-specific omissions; it performs no OCR.

Container controls follow the [Docker run reference](https://docs.docker.com/reference/cli/docker/container/run/) and [tmpfs resource guidance](https://docs.docker.com/engine/storage/tmpfs/). The application adds checksums, bounded output and durable stage fencing; Docker itself does not provide those application invariants.

## Compose service

The full local stack uses a private Unix-socket supervisor so no application container needs the host Docker socket. [ADR023](../../docs/adr/023-networkless-compose-parser-service.md) specifies the different process/container boundary and its deployment requirements. Each document runs in a fresh child with distinct UID65532, no capabilities, an additional seccomp filter, no writable paths, one inherited read-only source descriptor, hard resource limits and disconnect/deadline cleanup. The service has `network_mode: none` and `ipc: none`; only ingestion mounts `/run/parser` read-only.

The source lock covers parser and isolation modules, Dockerfile and hash-locked dependencies. After reviewing a source change, regenerate the lock and build from the repository root:

```sh
.venv/bin/python infra/parser/lock_service.py
.venv/bin/python infra/parser/build_service.py
.venv/bin/pytest -q tests/unit/ingestion/test_parser_service_protocol.py tests/integration/ingestion/test_parser_service.py
```

The build helper sends only locked files to Docker and writes the exact local image ID into `service-build.lock.json`. Tests reject stale source identity and use this image ID rather than trusting a mutable tag. The Compose root-context build uses `infra/parser/Dockerfile.service`; keep `.dockerignore` restricted to reviewed inputs. The runtime image verifies source identity before listening, and its health command is `python -m evidencedesk.ingestion.parser_service --health`.

Ingestion selects this implementation only with `ED_PARSER_SOCKET_PATH=/run/parser/parser.sock`; the optional lock path is `ED_PARSER_SERVICE_LOCK=infra/parser/service.lock.json`. Without the socket setting, the existing per-document Docker sandbox remains active. Never publish the Unix socket through an HTTP bridge or mount it in the frontend/API/chat worker.

The [2026-10-19 boundary report](../../docs/10-testing/parser-service-report.md) records real container/kernel verification and retained JUnit evidence. `scripts/security_audit.py` separately verifies and scans this service's immutable build identity; it rechecks both locks and all source inputs after scans. A successful parser test does not clear the vulnerability release gate.
