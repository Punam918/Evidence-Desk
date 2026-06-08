.PHONY: install check test build contracts
install:
	uv sync --all-packages --all-extras --locked
	./scripts/pnpm install --frozen-lockfile
check:
	uv run --no-sync ruff check apps/api tests evals/curation evals/runner
	uv run --no-sync ruff format --check apps/api tests evals/curation evals/runner
	uv run --no-sync mypy apps/api/src evals/curation evals/runner
	./scripts/pnpm typecheck
	./scripts/pnpm --filter @evidencedesk/web format:check
test: parser-image
	uv run --no-sync pytest tests -q
build:
	./scripts/pnpm build
contracts:
	./scripts/pnpm api:generate
	uv run --no-sync python scripts/validate_specs.py

.PHONY: parser-image
parser-image:
	uv run --no-sync python scripts/build_parser_image.py

.PHONY: monitoring-check security-audit
monitoring-check:
	uv run --no-sync python scripts/validate_monitoring.py
security-audit:
	uv run --no-sync python scripts/security_audit.py --bootstrap
