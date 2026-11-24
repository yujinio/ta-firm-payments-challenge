TEST_DB_URL = postgresql+psycopg://postgres:postgres@db:5432/firm_payments_test
LOAD_DB_URL = postgresql+psycopg://postgres:postgres@db:5432/firm_payments
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

test:
	docker compose run --rm --build -e TEST_DATABASE_URL=$(TEST_DB_URL) app pytest

lint:
	docker compose run --rm --build app sh -c "ruff check . && ruff format --check . && mypy app"

check: test lint

# Inside the Compose network, through the balancer. The script adds firms with ids
# from 1000 and payments between them; `make reset` returns to the seed.
load:
	docker compose run --rm --build app python scripts/load.py \
		--base-url http://lb --database-url $(LOAD_DB_URL) $(ARGS)
