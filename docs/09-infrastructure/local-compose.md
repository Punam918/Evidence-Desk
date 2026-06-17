# Local Compose installation

The root Compose stack includes the React frontend/Caddy, FastAPI, ingestion/index/chat/brief/conversation-purge workers, PostgreSQL, Qdrant, SeaweedFS, Keycloak, Ollama and local model readiness. No paid provider key is required. Embeddings, reranking, tokenizers and Qwen generation weights are downloaded and checked against the repository locks by a separate initialization job.

Integrated verification is underway. Native HTTPS/OIDC/CSRF/logout and real browser login→workspace→KB→upload→published revision have passed. Generation, retained-volume restart and a fully cold model download must be recorded separately before declaring the entire quickstart verified. Final UI/portfolio media and the final root README remain downstream deliverables.

From the repository root, with Docker Engine and a recent Compose supporting volume subpaths:

```bash
docker compose up --build -d
docker compose ps -a
docker compose logs -f models-init model-health
```

Initialization containers exiting0 is expected. A failed schema/storage/model job prevents its dependent workers from starting. The API can serve workspace administration while the model downloads or fails readiness. First startup needs network access to the pinned public images/model sources; serving workers themselves have no external network. Preserve the named model volumes to avoid repeated downloads. The current8B CPU configuration allocates a9GiB limit to Ollama plus separate memory for encoders, reranking and services; use a dedicated machine with at least24GiB RAM and adequate free disk, and measure your actual workload. These are starting requirements, not a concurrency guarantee.

The local address is **https://evidencedesk.localhost:8443**. Only this loopback port is published. The application and identity provider share this exact HTTPS origin; PostgreSQL/Qdrant/storage/identity/model ports are private. Export the generated public CA and trust it in your browser's certificate manager before normal browsing:

```bash
docker compose cp bootstrap:/secrets/trust/ca.crt ./evidencedesk-local-ca.crt
```

Bootstrap does not install certificates into your OS. Use the browser/OS's documented trust installation procedure, and trust this local CA only for this local installation. Never export the CA private key. Removing and recreating the secrets volume changes the CA and credentials, so a browser with the old CA will need its trust updated.

Sign in as `demo`. Display its generated local password in your own terminal:

```bash
docker compose run --rm --no-deps --entrypoint cat bootstrap /secrets/operator/demo_password
```

The password is created once and retained. It is not a checked-in demo password. Keycloak self-registration and direct password grants are disabled; the app uses authorization code/PKCE, secure server sessions and CSRF validation. The locally seeded account is for the portfolio installation; replace local identity provisioning before any public deployment. Hosted model keys are absent by default and must remain mounted server-side secrets if an operator deliberately configures them.

Ordinary shutdown keeps all data, users, secrets and models:

```bash
docker compose down
docker compose up -d
```

`docker compose down --volumes` is a destructive reset of this project's databases, documents, identity, credentials and model cache. It is not a repair step for failed migrations or a normal upgrade. Never use broad host cleanup commands on a shared development machine.

For source-bound image evidence, use `scripts/application_images.py` and the parser's `infra/parser/build_service.py`; `docker compose build` alone does not refresh those release manifests. The supply-chain audit checks the final application/web/identity/parser image IDs and source locks. Existing unresolved image advisory findings remain a release gate; local functional startup does not clear them.

The live protocol and browser checks use actual services:

```bash
.venv/bin/python scripts/validate_compose.py --output docs/10-testing/compose-auth-NEW.json
.tools/node-v24.21.0-linux-x64/bin/node scripts/validate_compose_browser.mjs \
  --scope published --output docs/10-testing/compose-browser-NEW.json
```

The protocol smoke verifies the exact CA and issuer. The isolated browser smoke explicitly bypasses browser certificate prompts for testing only, and records that limitation; it does not change application TLS verification or host trust. Both create authored synthetic test data. Reports omit passwords, tokens, cookies and auth responses. Do not use these diagnostics as evidence of final visual design or model quality.

If startup fails, inspect the specific initializer/service status first. Read-only parser failures must be fixed within the [isolated execution design](../adr/023-networkless-compose-parser-service.md); do not mount a Docker socket or enable privileged containers. Model digest drift requires an explicit artifact review, not a changed expected hash. A red model-health status keeps that model unavailable to users. Check `docs/11-implementation-plan/continuation.md` for open acceptance work and known integration issues.
