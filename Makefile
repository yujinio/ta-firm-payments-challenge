# Python tooling runs on the host through uv; the stand and its database run in Compose.
# DB_PORT and LB_PORT match the host ports docker-compose.yml publishes.
DB_PORT ?= 5432
LB_PORT ?= 8000
LOAD_DB_URL = postgresql+psycopg://postgres:postgres@localhost:$(DB_PORT)/firm_payments
# make load ARGS="--scenario hot-payer --requests 2000 --concurrency 32"
ARGS ?= --scenario uniform --requests 2000 --concurrency 32

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

# The tests use the firm_payments_test database on localhost:$(DB_PORT) (tests/conftest.py).
test:
	docker compose up -d --wait db
	uv run --locked pytest

lint:
	uv run --locked ruff check .
	uv run --locked ruff format --check .
	uv run --locked mypy app scripts migrations

check: test lint

# Through the balancer's host port. The script adds firms with ids from 1000 and
# payments between them; `make reset` returns to the seed.
load:
	uv run --locked python scripts/load.py \
		--base-url http://localhost:$(LB_PORT) --database-url $(LOAD_DB_URL) $(ARGS)
