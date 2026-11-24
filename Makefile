TEST_DB_URL = postgresql+psycopg://postgres:postgres@db:5432/firm_payments_test

.PHONY: up seed reset test lint check load

# nginx resolves the replicas only at start, and recreated replicas get new
# addresses, so the balancer is restarted after every up.
up:
	docker compose up --build -d --wait
	docker compose restart lb

seed:
	docker compose exec -T db psql -U postgres -d firm_payments < scripts/seed.sql

reset:
	docker compose exec -T db psql -U postgres -d firm_payments -v ON_ERROR_STOP=1 \
		-c "TRUNCATE payments, idempotency_keys, firms RESTART IDENTITY CASCADE" -f - < scripts/seed.sql

test:
	docker compose run --rm --build -e TEST_DATABASE_URL=$(TEST_DB_URL) app pytest

lint:
	docker compose run --rm --build app sh -c "ruff check . && ruff format --check . && mypy app"

check: test lint

load:
	uv run python scripts/load.py
