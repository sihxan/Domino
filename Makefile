.PHONY: test up down migrate seed deploy golden report-dry-run

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
	uv run alembic upgrade head
	uv run python -c "from domino_graph.ddl import apply_ddl; print(f'neo4j ddl: {apply_ddl()} statements')"

# Week 1: sources only; theses/ and storylines/ are loaded in later weeks.
seed:
	uv run python -m domino_ingest.sources

# Register the Prefect deployments from inside the worker (paths match its image). After a code change: make up, then make deploy.
deploy:
	docker compose exec prefect-worker python flows/deploy.py

# Gold-set extraction regression: real LLM calls, cost incurred (budget:golden). Only this file: tests/golden/test_llm_smoke.py
# is a one-off hand check, not part of the gold run. Fails with a message, before any LLM call, if the gold file is missing.
golden:
	uv run pytest tests/golden/test_extract_regression.py -m golden -s

report-dry-run:
	@echo "make report-dry-run: not implemented yet" && exit 1
