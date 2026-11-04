.PHONY: test up migrate seed golden report-dry-run

test:
	uv run ruff check .
	uv run mypy --strict
	uv run lint-imports
	uv run pytest tests/unit tests/integration

up:
	@echo "make up: implemented in week-1 T4" && exit 1

migrate:
	@echo "make migrate: implemented in week-1 T5" && exit 1

seed:
	@echo "make seed: implemented in week-1 T7" && exit 1

golden:
	@echo "make golden: not implemented yet" && exit 1

report-dry-run:
	@echo "make report-dry-run: not implemented yet" && exit 1
