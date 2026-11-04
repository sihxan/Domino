.PHONY: test up down migrate seed golden report-dry-run

test:
	uv run ruff check .
	uv run mypy --strict
	uv run lint-imports
	uv run pytest tests/unit tests/integration

# Infra services only; `app` profile (api, web, agent-runner, caddy) is not started here.
# Wait for the services later tasks depend on; embedder (slow model download, amd64 emulation on arm64) is not awaited.
WAIT_SERVICES = postgres neo4j redis minio prefect-server prefect-worker langfuse

up:
	docker compose up -d --build
	docker compose up -d --wait --wait-timeout 600 $(WAIT_SERVICES)
	docker compose ps

down:
	docker compose --profile app down

migrate:
	@echo "make migrate: implemented in week-1 T5" && exit 1

seed:
	@echo "make seed: implemented in week-1 T7" && exit 1

golden:
	@echo "make golden: not implemented yet" && exit 1

report-dry-run:
	@echo "make report-dry-run: not implemented yet" && exit 1
