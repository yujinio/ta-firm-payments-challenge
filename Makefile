TEST_DB_URL = postgresql+psycopg://postgres:postgres@db:5432/firm_payments_test

.PHONY: up seed test lint check load

up:
	docker compose up --build -d --wait

seed:
	docker compose exec -T db psql -U postgres -d firm_payments < scripts/seed.sql

test:
	docker compose run --rm -e TEST_DATABASE_URL=$(TEST_DB_URL) app pytest

lint:
	docker compose run --rm app sh -c "ruff check . && ruff format --check . && mypy app"

check: test lint

load:
	uv run python scripts/load.py
