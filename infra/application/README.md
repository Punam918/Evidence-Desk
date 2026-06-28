# Application images

The Python API/worker, Caddy-served React and optimized Keycloak images are part of T30 packaging. The root Compose stack is now implemented and undergoing integrated validation; the complete startup acceptance is not yet claimed. Mandatory full-stack/UI/live-media acceptance is tracked in [final delivery](../../docs/11-implementation-plan/final-delivery.md).

Build and record source-bound application/web/identity image IDs from the repository root:

```bash
.venv/bin/python scripts/application_images.py
.venv/bin/python scripts/application_images.py --check
```

`Dockerfile.api` uses the existing frozen uv workspace and all runtime extras, including CPU Torch and native provider adapters, without development tools. The application source is immutable inside the image and registered with the virtual environment so the isolated reranker subprocess can import it. It contains migrations/configuration/prompt resources and bootstrap scripts, but no model weights, host credentials or local caches. API is the default command; separate worker services override the command. Worker deployments must override the API-specific healthcheck with their appropriate lifecycle/readiness checks. The ingestion worker additionally needs the reviewed parser execution boundary; this image does not grant it a host Docker socket or include the Docker CLI.

`Dockerfile.web` builds React with the frozen pnpm lock and serves only static output through Caddy as UID10001. Its internal HTTP listener is8080. `/v1/*`, `/auth/*`, `/health/*` and `/identity/*` retain their original paths through the proxy; SSE flushing is explicit. Missing assets return404 instead of the HTML entry point. Hashed assets are immutable-cacheable; the HTML entry point is revalidated. `API_UPSTREAM` and `IDENTITY_UPSTREAM` are deployment-owned Caddy settings, defaulting to `api:8080` and `identity:8080`; they are not browser configuration. Local/public TLS belongs to the integrated deployment, not this internal listener.

The root `.dockerignore` allowlists required build inputs and excludes secrets, corpora, host virtual environments, node_modules and model caches. Digested base/build inputs are recorded in `images.lock.json`. `build.lock.json` binds both output image IDs to the exact runtime/build inputs; changed source/configuration requires a rebuild. The smoke validator and supply-chain audit consume these exact IDs, not mutable tags. No hosted provider secret is a Docker build argument. Runtime processes use UID10001, with read-only-root/drop-capability checks in the packaging smoke.

With both images built and the already-pinned tokenizer/embedding/reranker artifacts prepared locally, run:

```bash
.venv/bin/python scripts/validate_application_images.py \
  --output docs/10-testing/application-images-NEW.json
```

The validator runs real read-only/nonroot containers, executes dense/sparse embedding and the isolated reranker offline, verifies tokenizers/parser input identity, and checks real Caddy routing/static/SSE behavior against an explicitly scripted upstream. It removes only its uniquely named containers/network. It does not establish full API/identity integration, local generation, first-install download behavior or live model answer quality. The final README must use the eventual integrated browser workflow and final UI captures.

Remaining T30 work: complete Compose services and default local generation; per-service secrets and database roles; idempotent business/saver/object/identity/model bootstrap; isolated parser capability; TLS/issuer routing; maintenance scheduling; measured readiness/resource limits; fresh-install and retained-volume restarts; scanner/SBOM and existing release gates. No `docker compose up` success is claimed by building these two images.

Checkpoint2026-10-19: both images built and the [container smoke report](../../docs/10-testing/application-images-2026-10-19.json) passed on Linux/amd64. [Nine provenance/security tests](../../docs/10-testing/application-image-provenance-junit.xml) pass. The first web smoke attempt exposed the test network's lack of a published port; the final routing check uses a dedicated ordinary bridge and a loopback-only binding. Inference still runs with networking disabled. No unrelated host containers are modified.

Startup design follows Docker's [health/completion dependency conditions](https://docs.docker.com/compose/how-tos/startup-order/). Frozen dependency layering follows [uv's Docker guidance](https://docs.astral.sh/uv/guides/integration/docker/). The project-specific controls above require the recorded local checks in addition to those general recommendations.

Integrated setup uses [root compose.yaml](../../compose.yaml) and [ADR024](../../docs/adr/024-local-compose-bootstrap-and-identity.md). The dedicated parser service follows [ADR023](../../docs/adr/023-networkless-compose-parser-service.md); it has no Docker socket or network and has its own source/image lock. Local model initialization and periodic readiness are described in [MODELS.md](../bootstrap/MODELS.md). Fresh native database and private-object setup have passed; final browser/generation/restart acceptance is tracked separately.
