# Firm Payments Bulk Payment Service Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a FastAPI service with one endpoint, `POST /bulk-payments`, that atomically applies a firm's batch of payments or rejects the whole batch.

**Architecture:** A synchronous FastAPI app over PostgreSQL. One database transaction per request: claim the idempotency key, lock every firm in the batch with a single `ORDER BY id ... FOR NO KEY UPDATE` query, check the balance in Python, insert payments, update balances, commit. No in-process state; all coordination is in the database. The run stand is Docker Compose: PostgreSQL, a one-shot migration service, three application replicas and an nginx balancer.

**Tech Stack:** Python 3.12+, FastAPI, Uvicorn, Pydantic v2, pydantic-settings, SQLAlchemy 2.x (sync), psycopg 3, Alembic, PostgreSQL 18, pytest, httpx, ruff, mypy, uv, Docker Compose, nginx.

**Spec:** `docs/spec.md`, revision 3 (read it in full before any task; section numbers below refer to it).

**Location in repository:** `docs/plan.md`

## Global Constraints

- Python 3.12 or newer; dependency versions pinned by `uv.lock`, taken current at project creation.
- `float` is never used for money, in code or tests.
- Columns and types of `firms` and `payments` are exactly those in spec section 7.1.
- `INT32_MAX = 2_147_483_647` is the bound for every amount, batch total and balance.
- Every rejection caused by the request is HTTP 422 with body `{"error": {"code": ..., "message": ...}}`.
- Inside the transaction there are only database queries. Parsing, validation and hashing happen before it.
- No firm row is read before the locking query.
- Tests run against real PostgreSQL only. The schema under test is created by `alembic upgrade head`, never by `metadata.create_all`.
- Defaults: `MAX_PAYMENTS_PER_REQUEST=1000`, `MAX_DESCRIPTION_LENGTH=500`, `LOCK_TIMEOUT_MS=5000`, `DB_POOL_SIZE=10`, `DB_POOL_TIMEOUT_S=5`.
- The run stand has four services: `db`, `migrate` (one-shot), `app` (three replicas, one Uvicorn process each, no published port) and `lb` (nginx, published on host port 8000). Replicas never run migrations.
- `app/api.py` contains no SQL; `app/service.py` imports nothing from FastAPI.
- Code, comments, commit messages and README are in English. One logical change per commit.
- If the spec is contradictory, incomplete or looks wrong: stop and report. Do not fix it silently.

## Spec Revision

This plan implements revision 3 of the spec. The spec already contains the rules found while planning (no U+0000 in descriptions, the key character set, the 11-character cap on amounts, the thread limit, the `JSON` column type), the results of the design probes (section 17), the run stand (section 5.6) and the justified batch limit (section 5.7). Nothing in this plan overrides the spec.

## Review Focus

Inputs the spec implies but its test tables do not exercise, most likely first. Each has a test in the task named.

1. A `description` containing U+0000: expect 422 `validation_error`, not 500 (Task 4).
2. A body that is valid JSON but not an object (`[]`, `"x"`, `null`, `123`), an empty body, or a non-JSON content type: expect 422 in the single error format (Task 5).
3. Each amount fits `INT32_MAX` but the batch total does not: expect 422 `validation_error` before any database access (Task 4).
4. An `Idempotency-Key` that is empty, 256 characters long, or contains a space or non-ASCII character: expect 422 `validation_error` (Task 4).
5. An `amount` of thousands of digits: expect a fast 422, no numeric conversion attempted (Task 3).

## File Structure

| File | Responsibility |
|---|---|
| `app/config.py` | `Settings`, `get_settings()` |
| `app/db.py` | Engine, session factory, FastAPI session dependency |
| `app/models.py` | ORM models `Firm`, `Payment`, `IdempotencyKey` |
| `app/errors.py` | Domain error hierarchy with `code` and `status_code` |
| `app/schemas.py` | Amount parsing and formatting; request and response models |
| `app/idempotency.py` | Request hash; claiming a key; storing the response |
| `app/service.py` | `build_command` (pure, pre-transaction) and `execute_bulk_payment` (the transaction) |
| `app/api.py` | Routes: HTTP to service and back |
| `app/main.py` | App factory, exception handlers, lifespan |
| `migrations/versions/0001_initial.py` | The three tables, DDL verbatim from spec section 7 |
| `scripts/seed.sql` | The `INSERT` from the task, unchanged |
| `scripts/init-test-db.sql` | `CREATE DATABASE firm_payments_test;` for the compose database |
| `deploy/nginx.conf` | Balancer: one upstream `app:8000`, headers `X-Upstream` and `X-Request-Id` |
| `sample-request.json` | The request on page 4 of the task, verbatim |
| `scripts/load.py` | Load scenarios L1 |
| `tests/conftest.py` | Migration, cleanup, shared helpers and constants |
| `tests/unit/`, `tests/db/`, `tests/api/`, `tests/concurrency/` | Tests by level |
| `docs/verification-notes.md` | Mutation-check results, load numbers, outcome of the claims to re-confirm |

## Task Ownership

| Tasks | Agent workflow step | Model |
|---|---|---|
| 1–2 | Step 2, scaffold | Sonnet 5.5 |
| 3–6 | Step 3, core | Opus 5.5 |
| 7–8 | Step 4, concurrency and load | Opus 5.5 |
| 9 | Steps 5–6, review and README | Fable 5.2 in a new session; the author |

---

### Task 1: Runnable skeleton and test harness

**Files:**
- Create: `pyproject.toml`, `uv.lock`, `.gitignore`, `Dockerfile`, `docker-compose.yml`, `Makefile`, `.github/workflows/ci.yml`, `scripts/init-test-db.sql`, `deploy/nginx.conf`, `ai-logs/.gitkeep`
- Create: `app/__init__.py`, `app/config.py`, `app/db.py`, `app/api.py`, `app/main.py`
- Test: `tests/conftest.py`, `tests/api/test_healthz.py`

**Interfaces:**
- Produces:
  - `app.config.Settings(BaseSettings)` with fields `database_url: str`, `max_payments_per_request: int = 1000`, `max_description_length: int = 500`, `lock_timeout_ms: int = 5000`, `db_pool_size: int = 10`, `db_pool_timeout_s: int = 5`; `get_settings() -> Settings` (cached).
  - `app.db.get_engine() -> Engine` (cached); `app.db.get_session() -> Iterator[Session]` (FastAPI dependency, `expire_on_commit=False`, closes the session).
  - `app.main.create_app() -> FastAPI`; module-level `app`.
  - `app.api.router` with `GET /healthz`.
  - Make targets: `up`, `seed`, `test`, `lint`, `check`, `load`.
  - Database URLs: app `postgresql+psycopg://postgres:postgres@db:5432/firm_payments`; tests read `TEST_DATABASE_URL`, default `postgresql+psycopg://postgres:postgres@localhost:5432/firm_payments_test`.

- [ ] **Step 1: Write the failing test** in `tests/api/test_healthz.py`

```python
def test_healthz_reports_ok(client):
    r = client.get("/healthz")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}
```

`tests/conftest.py` sets `os.environ["DATABASE_URL"]` from `TEST_DATABASE_URL` before importing `app`, and provides `client` as `TestClient(app, raise_server_exceptions=False)`.

- [ ] **Step 2: Run it.** `uv run pytest tests/api/test_healthz.py -v`. Expected: FAIL, `app` not importable.

- [ ] **Step 3: Implement the skeleton.**
  - Engine: `create_engine(url, pool_size=settings.db_pool_size, max_overflow=0, pool_timeout=settings.db_pool_timeout_s, pool_pre_ping=True)`.
  - `GET /healthz` runs `SELECT 1`; returns `{"status": "ok"}`, or 503 `{"status": "unavailable"}` if the query fails.
  - Lifespan sets the AnyIO default thread limiter's `total_tokens` to `settings.db_pool_size` (spec section 10). This works only from the event loop; calling the limiter from a worker thread raises.
  - Compose services (spec section 5.6):
    - `db`: `postgres:18`, mounts `scripts/init-test-db.sql` into `/docker-entrypoint-initdb.d/`, healthcheck with `pg_isready`.
    - `app`: `deploy.replicas: 3`; command `uvicorn app.main:app --host 0.0.0.0 --port 8000` (one process per replica); no `ports`; waits for `db` healthy; healthcheck `python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/healthz')"`.
    - `lb`: `nginx:stable`, mounts `deploy/nginx.conf`, publishes `8000:80`, waits for `app` healthy.
    - The `migrate` service arrives in Task 2.
  - `deploy/nginx.conf`: an `upstream` whose single entry is `server app:8000;` (the name resolves to all replicas). In `location /`: `proxy_pass` to that upstream, `proxy_connect_timeout 2s`, `proxy_set_header X-Request-Id $request_id`, `add_header X-Upstream $upstream_addr always`. Leave `proxy_next_upstream` at its default: it does not re-send a `POST` that was already sent to a replica.
  - The image installs with `uv sync --frozen`, dev dependencies included, so tests run in it.
  - `make test` runs `docker compose run --rm -e TEST_DATABASE_URL=postgresql+psycopg://postgres:postgres@db:5432/firm_payments_test app pytest`; `make lint` runs `ruff check .`, `ruff format --check .`, `mypy app`; `make check` runs both.
  - CI workflow: check out, `docker compose up -d db`, `make check`.

- [ ] **Step 4: Run the test.** Expected: PASS.

- [ ] **Step 5: Verify the stand (spec checks S1 and S2).** `docker compose up --build -d --wait`, then `docker compose ps -a`. Expected: `db` and `lb` running, three `app` containers healthy. Then:

```bash
for i in $(seq 30); do curl -s -o /dev/null -D - localhost:8000/healthz | grep -i '^x-upstream'; done | sort | uniq -c
```

Expected: exactly three distinct addresses. If the installed Compose does not honour `deploy.replicas` (fewer than three `app` containers), stop and report. Then `make check`. Expected: exit code 0.

- [ ] **Step 6: Commit.** `git commit -m "chore: project skeleton, compose, healthz and test harness"`

---

### Task 2: Database schema

**Files:**
- Create: `alembic.ini`, `migrations/env.py`, `migrations/versions/0001_initial.py`, `app/models.py`, `scripts/seed.sql`, `sample-request.json`
- Modify: `tests/conftest.py`, `docker-compose.yml` (add the `migrate` service)
- Test: `tests/db/test_schema.py`

**Interfaces:**
- Consumes: `get_settings`, `get_engine` from Task 1.
- Produces:
  - `app.models.Base`, `Firm` (`id`, `name`, `balance_cents`, `uuid`), `Payment` (`id`, `payer_firm_id`, `payee_firm_id`, `amount_cents`, `description`), `IdempotencyKey` (`id`, `payer_firm_uuid`, `key`, `request_hash`, `response_body`, `created_at`); table names `firms`, `payments`, `idempotency_keys`.
  - In `tests/conftest.py`:
    - constants `PINECREST = "3f1c9a2e-7b4d-4c1e-9a55-2d8e6f0b7c41"`, `LOPEZ = "8b2e4c71-0d3a-4f6e-b1c9-5a7d2e9f4c10"`, `NAIR = "e5f18b3c-2a9d-4c07-8e6b-1d4a7f9c3b25"`, and `SAMPLE_REQUEST` (loaded from `sample-request.json`, which holds the request on page 4 of the task verbatim);
    - fixture `db -> Session`;
    - `balances(db) -> dict[str, int]` (uuid to cents, all firms);
    - `payment_rows(db) -> list[tuple[int, int, int, str]]` (payer id, payee id, cents, description; ordered by `id`);
    - `add_firm(db, firm_id: int, balance_cents: int) -> str` (returns a new lowercase UUID);
    - `set_balance(db, uuid: str, balance_cents: int) -> None`;
    - `bulk(payer: str, *payments: tuple[str, str]) -> dict` (builds a request body from `(payee_uuid, amount)` pairs, description `"p{i}"`).
  - Session-scoped autouse fixture runs `alembic upgrade head`; function-scoped autouse fixture runs `TRUNCATE payments, idempotency_keys, firms RESTART IDENTITY CASCADE` and then executes `scripts/seed.sql`.

- [ ] **Step 1: Write the failing tests**

```python
def test_seed_matches_task(db):
    assert balances(db) == {PINECREST: 5_000_000, LOPEZ: 50_000, NAIR: 200_000}

def test_firm_uuid_is_unique(db):
    with pytest.raises(IntegrityError):
        db.execute(text("INSERT INTO firms (id, name, balance_cents, uuid) VALUES (9, 'x', 0, :u)"), {"u": PINECREST})

def test_payment_requires_existing_firms(db):
    with pytest.raises(IntegrityError):
        db.execute(text("INSERT INTO payments (payer_firm_id, payee_firm_id, amount_cents, description) VALUES (1, 999, 1, 'x')"))

def test_idempotency_key_is_unique_per_payer(db):
    insert = text("INSERT INTO idempotency_keys (payer_firm_uuid, key, request_hash) VALUES (:p, 'k', 'h')")
    db.execute(insert, {"p": PINECREST})
    db.execute(insert, {"p": LOPEZ})          # same key, other payer: allowed
    with pytest.raises(IntegrityError):
        db.execute(insert, {"p": PINECREST})

def test_migration_round_trip():
    # alembic downgrade base, then upgrade head, both without error
```

- [ ] **Step 2: Run them.** `uv run pytest tests/db -v`. Expected: FAIL, tables do not exist.

- [ ] **Step 3: Implement.** The migration issues the three `CREATE TABLE` statements from spec section 7 verbatim through `op.execute`; downgrade drops the tables in reverse order. `migrations/env.py` takes the URL from `get_settings()`. `scripts/seed.sql` is the `INSERT` on page 4 of the task, unchanged. ORM models mirror the DDL; `response_body` is `sqlalchemy.JSON`, not `JSONB`. Add the Compose service `migrate`: the application image, command `alembic upgrade head`, waits for `db` healthy, no restart. `app` gains `depends_on: migrate` with `condition: service_completed_successfully`.

- [ ] **Step 4: Run the tests.** Expected: PASS, including Task 1's test.

- [ ] **Step 5: Verify the migration and seed path.** `docker compose up --build -d --wait`, then `docker compose ps -a`. Expected: `migrate` exited with code 0, three `app` containers healthy. Then `make seed` and `docker compose exec -T db psql -U postgres -d firm_payments -c "SELECT id, balance_cents FROM firms ORDER BY id"`. Expected: three rows with 5000000, 50000, 200000.

- [ ] **Step 6: Commit.** `git commit -m "feat: schema migration, ORM models and seed"`

---

### Task 3: Amount parsing, formatting and request hash

**Files:**
- Create: `app/schemas.py`, `app/idempotency.py`
- Test: `tests/unit/test_amount.py`, `tests/unit/test_request_hash.py`

**Interfaces:**
- Produces:
  - `app.schemas.INT32_MAX: int`
  - `app.schemas.parse_amount_cents(value: str) -> int`; raises `ValueError` for anything invalid.
  - `app.schemas.format_cents(cents: int) -> str`
  - `app.idempotency.compute_request_hash(payer_uuid: str, items: Sequence[tuple[str, int, str]]) -> str`; items are `(payee_uuid, amount_cents, description)` in request order.

- [ ] **Step 1: Write the failing tests**

```python
@pytest.mark.parametrize("text_, cents", [
    ("1200.75", 120_075), ("300", 30_000), ("5800.5", 580_050), ("0.01", 1),
    ("007", 700), ("21474836.47", 2_147_483_647),
])
def test_valid_amounts(text_, cents):
    assert parse_amount_cents(text_) == cents

@pytest.mark.parametrize("text_", [
    "0", "0.00", "-1", "+1", "1.234", "1.", ".5", "1e3", " 1", "1 ", "1\n",
    "1,5", "", "NaN", "١٢", "21474836.48", "9" * 5000,
])
def test_invalid_amounts(text_):
    with pytest.raises(ValueError):
        parse_amount_cents(text_)

@pytest.mark.parametrize("cents, text_", [(625_000, "6250.00"), (580_050, "5800.50"), (1, "0.01"), (1_325_125, "13251.25")])
def test_format_cents(cents, text_):
    assert format_cents(cents) == text_

def test_hash_ignores_amount_spelling():
    # hash built from parse_amount_cents("300") equals the one from parse_amount_cents("300.00")

def test_hash_is_sensitive():
    # differs when payer, payee, amount, description or the order of items changes

def test_hash_is_sha256_hex():
    assert re.fullmatch(r"[0-9a-f]{64}", compute_request_hash(PAYER, [(PAYEE, 100, "x")]))
```

- [ ] **Step 2: Run them.** `uv run pytest tests/unit -v`. Expected: FAIL, functions not defined.

- [ ] **Step 3: Implement.**
  - `parse_amount_cents`: reject if `len(value) > 11`; require `re.fullmatch(r"[0-9]+(\.[0-9]{1,2})?", value)`; compute cents with integer arithmetic on the split string; reject if not `0 < cents <= INT32_MAX`.
  - `format_cents`: integer arithmetic only, always two decimals.
  - `compute_request_hash`: SHA-256 hex digest of the UTF-8 bytes of

    ```python
    json.dumps({"payer": payer_uuid, "payments": [list(item) for item in items]},
               sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    ```

    This exact form is fixed: stored hashes must stay comparable.

- [ ] **Step 4: Run the tests.** Expected: PASS.

- [ ] **Step 5: Commit.** `git commit -m "feat: amount parsing, formatting and request hash"`

---

### Task 4: Request models, domain errors and command builder

**Files:**
- Create: `app/errors.py`, `app/service.py`
- Modify: `app/schemas.py`
- Test: `tests/unit/test_schemas.py`, `tests/unit/test_build_command.py`

**Interfaces:**
- Consumes: `parse_amount_cents`, `INT32_MAX`, `compute_request_hash`, `Settings`.
- Produces:
  - `app.errors.DomainError(Exception)` with class attributes `code: str`, `status_code: int = 422`, and instance attribute `message: str`. Subclasses and codes: `ValidationFailed` (`validation_error`, extra attribute `details: list[dict]`), `SelfPayment` (`self_payment`), `FirmNotFound` (`firm_not_found`), `InsufficientFunds` (`insufficient_funds`), `BalanceOverflow` (`balance_overflow`), `IdempotencyKeyReuse` (`idempotency_key_reuse`), `ServiceBusy` (`busy`, `status_code = 503`).
  - `app.schemas.PaymentIn` (`amount: str`, `payee_firm_uuid: UUID`, `description: str`), `BulkPaymentRequest` (`payer_firm_uuid: UUID`, `payments: list[PaymentIn]`). Both forbid unknown fields. `amount` and `description` accept JSON strings only. `amount` is validated with `parse_amount_cents`. `description` rejects U+0000. `payments` needs at least one element.
  - `app.service.PaymentItem` (frozen dataclass: `payee_uuid: str`, `amount_cents: int`, `description: str`).
  - `app.service.BulkPaymentCommand` (frozen dataclass: `payer_uuid: str`, `items: tuple[PaymentItem, ...]`, `total_cents: int`, `credits: dict[str, int]`, `firm_uuids: frozenset[str]`, `idempotency_key: str | None`, `request_hash: str | None`).
  - `app.service.build_command(request: BulkPaymentRequest, idempotency_key: str | None, settings: Settings) -> BulkPaymentCommand`. Pure; no database access.

- [ ] **Step 1: Write the failing tests**

`tests/unit/test_schemas.py`, each case expecting `pydantic.ValidationError` from `BulkPaymentRequest.model_validate(...)`: empty `payments`; unknown top-level field; unknown field inside a payment; `amount` given as the number `12.5`; `amount` `"1.234"`; `description` given as `5`; missing `description`; `description` `"a\u0000b"`; `payee_firm_uuid` `"not-a-uuid"`. One positive case: `SAMPLE_REQUEST` validates.

`tests/unit/test_build_command.py`:

```python
def test_sample_request_totals(settings):
    cmd = build_command(BulkPaymentRequest.model_validate(SAMPLE_REQUEST), None, settings)
    assert cmd.total_cents == 1_325_125
    assert cmd.credits == {NAIR: 1_205_050, LOPEZ: 120_075}
    assert cmd.firm_uuids == {PINECREST, NAIR, LOPEZ}
    assert [i.amount_cents for i in cmd.items] == [625_000, 580_050, 120_075]
    assert cmd.idempotency_key is None and cmd.request_hash is None

def test_uuids_are_lowercased(settings): ...            # upper-case input, lower-case payer_uuid and payee_uuid
def test_self_payment_rejected(settings): ...            # raises SelfPayment
def test_too_many_payments(settings): ...                # max_payments_per_request=2, three payments: ValidationFailed
def test_description_too_long(settings): ...             # 501 characters: ValidationFailed; 500: accepted
def test_total_overflow(settings): ...                   # two payments of "21474836.47": ValidationFailed

@pytest.mark.parametrize("key", ["", "a" * 256, "has space", "ключ"])
def test_invalid_idempotency_key(settings, key): ...     # ValidationFailed

def test_valid_key_produces_hash(settings):
    cmd = build_command(request, "a" * 255, settings)
    assert re.fullmatch(r"[0-9a-f]{64}", cmd.request_hash)
```

`settings` is a fixture returning `Settings(database_url="postgresql+psycopg://unused")`, with fields overridden per test.

- [ ] **Step 2: Run them.** Expected: FAIL, names not defined.

- [ ] **Step 3: Implement.** `build_command` performs, in order: payment count limit, description length limit, key format (`re.fullmatch(r"[\x21-\x7e]{1,255}", key)`), conversion to cents, self-payment check, total and its overflow check, per-payee credit sums, UUID set, hash when a key is present. `ValidationFailed.details` items have the shape `{"loc": [...], "msg": "...", "type": "..."}`.

- [ ] **Step 4: Run the tests.** Expected: PASS.

- [ ] **Step 5: Commit.** `git commit -m "feat: request models, domain errors and command builder"`

---

### Task 5: The payment transaction and the endpoint

**Files:**
- Modify: `app/service.py`, `app/api.py`, `app/main.py`
- Test: `tests/api/test_bulk_payments.py`, `tests/api/test_validation.py`

**Interfaces:**
- Consumes: everything from Tasks 1–4.
- Produces:
  - `app.service.BulkPaymentResult` (frozen dataclass: `body: dict[str, Any]`, `replayed: bool`).
  - `app.service.execute_bulk_payment(session: Session, command: BulkPaymentCommand, lock_timeout_ms: int) -> BulkPaymentResult`. Owns the transaction. Raises `FirmNotFound`, `InsufficientFunds`, `BalanceOverflow`, `ServiceBusy`.
  - `POST /bulk-payments`: 201 with the body from spec section 8.1.
  - Exception handlers in `create_app`: `DomainError` to its status and the error body (`details` included for `ValidationFailed`; header `Retry-After: 1` for `ServiceBusy`); `RequestValidationError` to 422 `validation_error` with `details` reduced to `loc`, `msg`, `type`; any other exception to 500 `internal_error` with no internals in the body.

- [ ] **Step 1: Write the failing tests** in `tests/api/test_bulk_payments.py`

```python
def test_a1_sample_request(client, db):
    r = client.post("/bulk-payments", json=SAMPLE_REQUEST)
    assert r.status_code == 201
    assert balances(db) == {PINECREST: 3_674_875, LOPEZ: 170_075, NAIR: 1_405_050}
    body = r.json()
    assert body["payer_firm_uuid"] == PINECREST
    assert body["total_amount"] == "13251.25"
    assert [p["amount"] for p in body["payments"]] == ["6250.00", "5800.50", "1200.75"]
    assert sorted(payment_rows(db)) == sorted([
        (1, 3, 625_000, "Overflow returns, August 2026"),
        (1, 3, 580_050, "Amended returns, August 2026"),
        (1, 2, 120_075, "Bookkeeping cleanup, 3 clients"),
    ])

def assert_rejected(r, code, db, before):
    assert r.status_code == 422
    assert r.json()["error"]["code"] == code
    assert balances(db) == before and payment_rows(db) == []
```

| Test | Request | Expectation |
|---|---|---|
| `test_a2_insufficient_funds` | Lopez pays Nair `"500.01"` | `assert_rejected(..., "insufficient_funds", ...)` |
| `test_a3_exact_balance` | Lopez pays Nair `"500"` | 201; Lopez 0, Nair 250 000 |
| `test_a4_one_cent_short` | Lopez pays Nair `"300"` and Pinecrest `"200.01"` | rejected, `insufficient_funds` |
| `test_a5_unknown_payee` | Pinecrest pays Nair `"1"` and a random UUID `"1"` | rejected, `firm_not_found` |
| `test_a6_unknown_payer` | random UUID pays Nair `"1"` | rejected, `firm_not_found` |
| `test_a7_self_payment` | Pinecrest pays Pinecrest `"1"` | rejected, `self_payment` |
| `test_a8_repeated_payee` | Pinecrest pays Nair `"1"`, `"2"`, `"3"` | 201; Nair 200 600; three rows in `payments` |
| `test_a10_uppercase_uuid` | `SAMPLE_REQUEST` with all UUIDs upper-cased | 201; response UUIDs lower-case |
| `test_a11_balance_overflow` | `set_balance(db, NAIR, INT32_MAX)`; Pinecrest pays Nair `"0.01"` | rejected, `balance_overflow` |
| `test_a13_response_order` | Pinecrest pays Lopez `"3"`, Nair `"1"`, Lopez `"2"` | response amounts `["3.00", "1.00", "2.00"]` |
| `test_response_has_no_balance` | `SAMPLE_REQUEST` | no key containing `balance` anywhere in the body |

In `tests/api/test_validation.py`:

| Test | Input | Expectation |
|---|---|---|
| `test_a9_schema_violations` (parametrized) | empty `payments`; extra field; amount as number; amount `"0"`; description of 501 characters; 1001 payments; description with U+0000 | 422, `validation_error`, `details` is a non-empty list |
| `test_non_object_bodies` (parametrized) | raw bodies `[]`, `"x"`, `null`, `123`, `{bad json`, empty body; a JSON body sent as `text/plain` | 422, `validation_error` |
| `test_a12_error_format` | one request per 422 code above | body keys are exactly `{"error"}`; `error` has `code` and `message` |
| `test_a12_internal_error_format` | dependency override of `get_session` raising `RuntimeError("boom")` | 500; `error.code == "internal_error"`; `"boom"` not in the response text |

- [ ] **Step 2: Run them.** `uv run pytest tests/api -v`. Expected: FAIL, 404 for `/bulk-payments`.

- [ ] **Step 3: Implement `execute_bulk_payment`** following spec section 8.2, transaction steps 1 and 3–8 and 10 (the key steps arrive in Task 6). Points the spec does not pin:
  - Open the transaction with `with session.begin():`.
  - Set the timeout with `SELECT set_config('lock_timeout', :v, true)` and `v = f"{lock_timeout_ms}ms"`. `SET LOCAL` cannot take a bound parameter.
  - Lock query: `select(Firm).where(Firm.uuid.in_(command.firm_uuids)).order_by(Firm.id).with_for_update(key_share=True).execution_options(populate_existing=True)`.
  - Order of checks: missing firms, then payer balance, then payee overflow.
  - Insert with `session.add_all(rows)` and `session.flush()`; read each `id` from its object to build the response in request order.
  - Update balances by assigning to the locked ORM objects.
  - Catch `sqlalchemy.exc.DBAPIError`: if `getattr(exc.orig, "sqlstate", None)` is `"55P03"` or `"40P01"`, raise `ServiceBusy` (log `40P01` at ERROR). Catch `sqlalchemy.exc.TimeoutError` (pool checkout) and raise `ServiceBusy`. Re-raise everything else.

- [ ] **Step 4: Implement the route and the handlers.** The route is a plain `def` that receives the session through `Depends(get_session)` and the settings through `Depends(get_settings)`; tests override both. It calls `build_command`, then `execute_bulk_payment`, and returns `JSONResponse(result.body, status_code=201)`. It emits one log record per request with the request id (from the `X-Request-Id` header, or generated), the instance (container host name), payer, payment count, total, outcome and duration, and never the descriptions (spec section 12).

- [ ] **Step 5: Run all tests.** `uv run pytest -v`. Expected: PASS.

- [ ] **Step 6: Verify by hand.** `docker compose up --build -d && make seed`, then post the sample request with `curl`. Expected: 201 and `"total_amount":"13251.25"`.

- [ ] **Step 7: Commit.** `git commit -m "feat: bulk payment transaction and endpoint"`

---

### Task 6: Idempotency

**Files:**
- Modify: `app/idempotency.py`, `app/service.py`, `app/api.py`
- Test: `tests/api/test_idempotency.py`

**Interfaces:**
- Consumes: `IdempotencyKey` model, `IdempotencyKeyReuse`, `BulkPaymentCommand`.
- Produces:
  - `app.idempotency.claim_key(session: Session, payer_uuid: str, key: str, request_hash: str) -> dict[str, Any] | None`. Returns `None` when the key was claimed by this transaction. Returns the stored response body when the key already exists with the same hash. Raises `IdempotencyKeyReuse` when the hash differs.
  - `app.idempotency.store_response(session: Session, payer_uuid: str, key: str, body: dict[str, Any]) -> None`.
  - The route reads the `Idempotency-Key` header and passes it to `build_command`. A replayed response carries the header `Idempotent-Replayed: true`.

- [ ] **Step 1: Write the failing tests**

```python
KEY = {"Idempotency-Key": "order-1"}

def test_i1_replay_returns_stored_response(client, db):
    first = client.post("/bulk-payments", json=SAMPLE_REQUEST, headers=KEY)
    second = client.post("/bulk-payments", json=SAMPLE_REQUEST, headers=KEY)
    assert first.status_code == second.status_code == 201
    assert second.content == first.content        # byte-identical replay
    assert "Idempotent-Replayed" not in first.headers
    assert second.headers["Idempotent-Replayed"] == "true"
    assert balances(db)[PINECREST] == 3_674_875
    assert len(payment_rows(db)) == 3
```

| Test | Scenario | Expectation |
|---|---|---|
| `test_i2_same_key_other_body` | sample request, then Pinecrest pays Nair `"1"`, same key | 422 `idempotency_key_reuse`; balances as after the first request |
| `test_i3_rejection_is_not_stored` | Lopez pays Nair `"600"` with a key: 422; `set_balance(db, LOPEZ, 60_000)`; same request, same key | second response 201 without `Idempotent-Replayed` |
| `test_i4_key_is_scoped_to_payer` | Pinecrest and Nair each pay Lopez `"1"` with the same key | both 201, neither replayed |
| `test_i6_no_key_executes_twice` | sample request twice, no header | Pinecrest 2 349 750; six rows in `payments` |
| `test_i7_key_table_state` | one accepted and one rejected request, different keys | exactly one row in `idempotency_keys`; its `response_body` is not null |
| `test_i8_amount_spelling` | Pinecrest pays Nair `"300"`, then `"300.00"`, same key | second is a replay; Nair credited once |
| `test_invalid_key_header` | header value of 256 characters | 422 `validation_error` |

- [ ] **Step 2: Run them.** Expected: FAIL, second request executes again.

- [ ] **Step 3: Implement.**
  - `claim_key`: `postgresql.insert(IdempotencyKey).values(...).on_conflict_do_nothing(index_elements=["payer_firm_uuid", "key"]).returning(IdempotencyKey.id)`. If no row comes back, select the existing row and compare hashes. An existing row with a null `response_body` is a broken invariant: raise `RuntimeError`.
  - In `execute_bulk_payment`: call `claim_key` right after setting the lock timeout and before the lock query. On a stored body, return `BulkPaymentResult(body, replayed=True)`. Call `store_response` as the last statement before commit.

- [ ] **Step 4: Run all tests.** Expected: PASS.

- [ ] **Step 5: Commit.** `git commit -m "feat: idempotency keys for bulk payments"`

---

### Task 7: Concurrency tests and the mutation check

**Files:**
- Create: `tests/concurrency/conftest.py`, `tests/concurrency/test_races.py`, `tests/concurrency/test_locks.py`, `docs/verification-notes.md`
- Do not modify anything under `app/`, except temporarily in Step 4.

**Interfaces:**
- Consumes: `build_command`, `execute_bulk_payment`, `get_engine`, the helpers from `tests/conftest.py`.
- Produces, in `tests/concurrency/conftest.py`:
  - `run_concurrently(fn: Callable[[int], T], n: int) -> list[T]`: `n` threads released together by a `threading.Barrier`; re-raises the first exception.
  - `call_service(payload: dict, key: str | None = None, lock_timeout_ms: int = 5000) -> tuple[str, dict | None]`: opens its own `Session`, runs `build_command` and `execute_bulk_payment`, returns `("created" | "replayed", body)` or `(error.code, None)`.
  - `assert_ledger_consistent(db, start: dict[str, int]) -> None`: no balance below zero; the sum of balances equals `sum(start.values())`; for every firm, balance equals start minus its `payments` as payer plus its `payments` as payee.
  - `wait_until_blocked(db, timeout_s: float = 5.0) -> None`: polls `pg_stat_activity` until another backend has `wait_event_type = 'Lock'`; fails the test on timeout.

In-process tests call the service from threads, each with its own session. HTTP-level concurrency is Task 8.

- [ ] **Step 1: Write the tests** (they are expected to pass against the existing code; Step 4 proves they can fail)

| Test | Setup and action | Expectation |
|---|---|---|
| `test_c1_one_payer_many_batches` | Firm P with 100 000 cents; 10 threads each send P pays Lopez `"100"` and Nair `"200"`; 20 rounds, state reset each round | exactly 3 `created`, 7 `insufficient_funds`; P ends at 10 000; 6 payment rows; ledger consistent |
| `test_c2_crossing_payments` | Firms A and B with 10 000 000 cents; 25 threads A pays B `"1"`, 25 threads B pays A `"1"`; 20 rounds | all 50 `created`; A and B unchanged; ledger consistent |
| `test_c3_cycle_and_random_mix` | Six firms; `random.Random(20261121)` generates 200 batches of 1–4 payments between random distinct firms, including the cycle A→B, B→C, C→A; 16 threads | outcomes only `created` or `insufficient_funds`; ledger consistent |
| `test_c6_hot_payee` | 20 payer firms with 100 000 cents each pay Nair `"10"` at once | all `created`; Nair equals 200 000 + 20 × 1 000 |
| `test_i5_same_key_in_parallel` | 8 threads send `SAMPLE_REQUEST` with one key; 10 rounds | exactly one `created`, seven `replayed`; all bodies equal; 3 payment rows; Pinecrest 3 674 875 |
| `test_c4_request_waits_for_row_lock` | A raw connection runs `BEGIN; SELECT 1 FROM firms WHERE id = 2 FOR UPDATE`. A thread sends Lopez pays Nair `"400"`. Main thread: `wait_until_blocked`; then on the raw connection `UPDATE firms SET balance_cents = 30000 WHERE id = 2; COMMIT` | the thread returns `insufficient_funds`; Lopez 30 000; no payment rows |
| `test_c5_lock_timeout` | Raw connection holds the lock on firm 2; `call_service(..., lock_timeout_ms=200)` | `busy`; nothing changed |
| `test_c5_http_503` | Same lock; `client.post` with `get_settings` overridden to `lock_timeout_ms=200` | 503; `error.code == "busy"`; header `Retry-After: 1` |
| `test_key_wait_respects_lock_timeout` | Raw connection inserts an `idempotency_keys` row for (Pinecrest, `"k"`) and does not commit; `call_service(SAMPLE_REQUEST, key="k", lock_timeout_ms=200)` | `busy`; nothing changed |
| `test_pool_exhaustion_is_busy` | An engine with `pool_size=1, max_overflow=0, pool_timeout=0.2`; one connection checked out; `execute_bulk_payment` on a session bound to that engine | raises `ServiceBusy` |

- [ ] **Step 2: Run them.** `uv run pytest tests/concurrency -v`. Expected: PASS. If `test_key_wait_respects_lock_timeout` fails, the spec's claim about `lock_timeout` on a unique-index wait is false: stop and report.

- [ ] **Step 3: Run the whole suite five times.** `for i in 1 2 3 4 5; do uv run pytest -q || break; done`. Expected: five clean runs.

- [ ] **Step 4: Mutation check.** Apply each mutation to `app/service.py` alone, run `uv run pytest tests/concurrency -v`, record which tests fail, then revert with `git checkout app/service.py`.
  - M1: remove `.with_for_update(key_share=True)` from the lock query. Expected: at least `test_c1_one_payer_many_batches` and `test_c4_request_waits_for_row_lock` fail.
  - M2: remove `.order_by(Firm.id)` from the lock query. Expected: `test_c2_crossing_payments` or `test_c3_cycle_and_random_mix` fails with `busy` outcomes caused by deadlocks. In the design probe this workload gave 80 deadlocks in 2,400 transactions.
  - M3: lock the payer row in one query, then the payee rows in a second query. Expected: `test_c2_crossing_payments` fails with `busy` outcomes caused by deadlocks.
  - If a mutation leaves every test green, the tests prove nothing: strengthen them (more rounds, more threads) and repeat.

- [ ] **Step 5: Write `docs/verification-notes.md`.** Sections: "Mutation check" (mutation, failing tests, as observed) and "Claims re-confirmed" (the five claims at the end of spec section 17, each with the test that settled it and the result).

- [ ] **Step 6: Confirm `git status` shows no change under `app/`, then commit.** `git commit -m "test: concurrency, locking and idempotency under parallel load"`

---

### Task 8: Multi-process test and load script

**Files:**
- Create: `tests/concurrency/test_multiprocess.py`, `scripts/load.py`
- Modify: `Makefile` (`load` target), `docs/verification-notes.md`

**Interfaces:**
- Consumes: helpers from `tests/conftest.py` and `tests/concurrency/conftest.py`.
- Produces:
  - Module-scoped fixture `live_server -> str` (base URL): starts `uvicorn app.main:app --port <free port> --workers 4` as a subprocess with `DATABASE_URL` set to the test database, polls `/healthz` for up to 10 seconds, terminates the process afterwards.
  - `scripts/load.py --base-url URL --database-url URL --scenario {uniform,hot-payer,hot-payee,max-batch} --requests N --concurrency C`.

- [ ] **Step 1: Write the tests**

| Test | Action | Expectation |
|---|---|---|
| `test_c7_one_payer_over_http` | Firm P with 100 000 cents; 10 threads, each with its own `httpx.Client`, post P pays Lopez `"100"` and Nair `"200"` | statuses: three 201, seven 422 with `insufficient_funds`; P ends at 10 000; ledger consistent |
| `test_c7_same_key_over_http` | 8 threads post `SAMPLE_REQUEST` with one key | all 201; exactly one response without `Idempotent-Replayed`; 3 payment rows |
| `test_c7_crossing_over_http` | Firms A and B with 10 000 000 cents; 25 threads post A pays B `"1"`, 25 threads post B pays A `"1"`; 10 rounds | all 201, no 503; A and B unchanged; ledger consistent |

- [ ] **Step 2: Run them.** `uv run pytest tests/concurrency/test_multiprocess.py -v`. Expected: PASS.

- [ ] **Step 3: Implement `scripts/load.py`.** It inserts its own firms (ids from 1000, 1 100 firms, 1 000 000 000 cents each) through `--database-url`, removing leftovers from earlier runs first. It sends batches of three payments, each between `"0.01"` and `"10.00"`, with `httpx` from a `ThreadPoolExecutor`. `uniform` picks payer and payees at random; `hot-payer` uses one payer; `hot-payee` puts one payee in every batch; `max-batch` sends requests of 1 000 payments of `"0.01"` from one payer to 1 000 distinct payees and also reports the median and maximum request duration next to the 250 ms budget of spec section 5.7. Output: requests per second, p50, p95, p99 in milliseconds, and a count per status code. After the run it checks that no balance is negative, that the sum of the firms' balances is unchanged, and that there were no 500 responses; it exits non-zero otherwise.

- [ ] **Step 4: Run the scenarios through the balancer.** `make load` runs the script inside the Compose network with `--base-url http://lb`. Run `uniform`, `hot-payer` and `hot-payee` with `--requests 2000 --concurrency 32`, and `max-batch` with `--requests 40 --concurrency 4`. Expected: exit code 0 for each.

- [ ] **Step 5: Stand check S3.** On a freshly seeded database stop one replica with `docker stop $(docker compose ps -q app | head -1)`. Post `sample-request.json` five times with the header `Idempotency-Key: s3`. Expected: five 201 responses, four of them with `Idempotent-Replayed: true`; Pinecrest balance 3674875. Then `docker compose up -d --wait` and `docker compose restart lb` to restore three replicas behind the balancer.

- [ ] **Step 6: Record the numbers** in `docs/verification-notes.md`: section "Load run" with the machine description and the command lines, including the `max-batch` duration against the 250 ms budget; section "Stand checks" with the outcome of S1–S3. The load figures have no pass/fail thresholds: they replace the estimates in spec assumption 7 and section 5.7 with measurements. If `max-batch` exceeds the budget, record it and report it; do not change the limit silently.

- [ ] **Step 7: Commit.** `git commit -m "test: multi-process HTTP concurrency, load script and stand checks"`

---

### Task 9: Acceptance run and submission

Owner: the author, with an independent review in a new session (agent workflow steps 5 and 6).

**Files:**
- Create: `README.md`
- Modify: `ai-logs/` (exported session logs, unedited)

- [ ] **Step 1: Independent review.** Run the step 5 prompt from the agent work plan in a new session that has only the repository. Triage every finding: fix, or record in the README why not.

- [ ] **Step 2: Write the README** as a pull request description: solution and approach, issues encountered, assumptions (spec section 4), possible improvements (spec section 15), how to run and verify, AI tools used and where each fit, answers to the two questions. Take measured facts from `docs/verification-notes.md`. The run section names the prerequisites (Docker with Compose; `make` optional), gives the plain `docker compose` command behind every `make` target, and states the endpoint as `POST http://localhost:8000/bulk-payments`.

- [ ] **Step 3: Acceptance run on a clean clone**, following only the README (spec section 14.2):

```bash
git clone <repo> fp-acceptance && cd fp-acceptance
docker compose up --build -d --wait
make seed
curl -s -o /dev/null -w "%{http_code}\n" -X POST localhost:8000/bulk-payments \
  -H "Content-Type: application/json" -d @sample-request.json
```

Expected: `201`; balances 3674875, 170075, 1405050. Repeat the `curl` three more times. Expected: `201`, `201`, `422`. Then `make check`. Expected: exit code 0. Finally repeat the upstream count from Task 1, Step 5. Expected: three distinct addresses.

`sample-request.json` was committed in Task 2.

- [ ] **Step 4: Submission checklist** (spec section 14.3): commit history shows the progress; `ai-logs/` is complete and unedited; README sections are all present; assumptions and improvements match the spec.

- [ ] **Step 5: Commit.** `git commit -m "docs: README, sample request and AI session logs"`

---

## Spec Coverage

| Spec item | Task |
|---|---|
| Sections 6, 10, 11, 12 (stack, configuration, layout, healthz) | 1 |
| Section 7 (schema) | 2 |
| Section 8.1 validation rules; tests U1–U5 | 3, 4 |
| Section 8.1 responses; section 8.2 steps 1, 3–8, 10; tests A1–A13 | 5 |
| Section 8.2 steps 2 and 9; tests I1–I4, I6–I8 | 6 |
| Section 9 invariants; tests C1–C6, I5 | 7 |
| Tests C7, L1 | 8 |
| Section 13.6 static checks and CI | 1 (`make check`, workflow) |
| Section 5.6 run stand; checks S1–S3 | 1 (S1, S2), 2 (`migrate`), 8 (S3) |
| Section 5.7 batch limit measurement | 8 (`max-batch`) |
| Section 14 validation | 9 |
| Section 17, five claims to re-confirm | 7 (Step 5 of that task records each) |

Per-request structured logging (spec section 12) is part of Task 5, Step 4. It has no dedicated test.

Spec test ids without an id in a test name:

| Spec id | Test |
|---|---|
| A14 | `test_non_object_bodies` |
| A15 | `test_response_has_no_balance` |
| I9 | `test_invalid_idempotency_key` (unit), `test_invalid_key_header` (HTTP) |
| C7 | `test_c7_one_payer_over_http`, `test_c7_crossing_over_http`, `test_c7_same_key_over_http` |
| C8 | `test_key_wait_respects_lock_timeout` |
| C9 | `test_pool_exhaustion_is_busy` |
| M1–M3 | Task 7, Step 4 |
| S1–S3 | Task 1 Step 5 (S1, S2), Task 2 Step 5 (S1 with `migrate`), Task 8 Step 5 (S3) |
