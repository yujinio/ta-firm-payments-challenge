# Firm Payments: a bulk payment service

A web service with one endpoint, `POST /bulk-payments`. A firm sends a batch of payments to other firms. If the payer's balance covers the whole batch, the service does everything in one PostgreSQL transaction and answers **201**: it inserts the payments, debits the payer and credits every payee. If the balance does not cover the batch, nothing changes and the service answers **422**.

The service runs as three replicas behind an nginx load balancer. Correctness across replicas comes from row locks in the database, not from anything held in a process. The implementation follows `docs/spec.md`, which records each design decision with the alternatives that were rejected. `docs/verification-notes.md` records the measured and observed results quoted below; the design-time probe figures come from spec section 17.

- [Run it](#run-it)
- [Verify it](#verify-it)
- [API](#api)
- [Solution and approach](#solution-and-approach)
- [Issues I ran into](#issues-i-ran-into)
- [Assumptions](#assumptions)
- [Known limitations and possible improvements](#known-limitations-and-possible-improvements)
- [How I worked](#how-i-worked)
- [Repository layout](#repository-layout)
- [Two questions](#two-questions)

## Run it

**Prerequisites:**

- Docker with Compose v2 or later: Docker Desktop, Docker Engine, or Podman with a `docker` command (podman-docker) and Docker Compose as its compose provider.
- `make`.
- [uv](https://docs.astral.sh/uv/), needed only for the tests and the load script. uv installs Python 3.14 itself if it is missing.

The balancer listens on `127.0.0.1:8000` and the database on `127.0.0.1:5432`. If either port is taken, export `LB_PORT` and/or `DB_PORT` with other values before running the commands, and use your port in the URLs below.

```bash
git clone https://github.com/yujinio/ta-firm-payments-challenge.git
cd ta-firm-payments-challenge
make up      # PostgreSQL, migrations, three app replicas, nginx on localhost:8000
make seed    # the three firms from the task (use make reset if they are already there)
```

Send the sample request from page 4 of the task:

```bash
curl -i -X POST http://localhost:8000/bulk-payments \
  -H 'Content-Type: application/json' \
  --data-binary @sample-request.json
```

```
HTTP/1.1 201 Created
X-Upstream: 10.89.2.136:8000

{"payer_firm_uuid":"3f1c9a2e-7b4d-4c1e-9a55-2d8e6f0b7c41","total_amount":"13251.25","payments":[{"id":1,"payee_firm_uuid":"e5f18b3c-2a9d-4c07-8e6b-1d4a7f9c3b25","amount":"6250.00","description":"Overflow returns, August 2026"},{"id":2,"payee_firm_uuid":"e5f18b3c-2a9d-4c07-8e6b-1d4a7f9c3b25","amount":"5800.50","description":"Amended returns, August 2026"},{"id":3,"payee_firm_uuid":"8b2e4c71-0d3a-4f6e-b1c9-5a7d2e9f4c10","amount":"1200.75","description":"Bookkeeping cleanup, 3 clients"}]}
```

Check the balances:

```bash
docker compose exec -T db psql -U postgres -d firm_payments \
  -c "SELECT id, name, balance_cents FROM firms ORDER BY id"
```

```
 id |        name         | balance_cents
----+---------------------+---------------
  1 | Pinecrest CPA Group |       3674875
  2 | Lopez Bookkeeping   |        170075
  3 | Nair Tax Services   |       1405050
```

These are $36,748.75, $1,700.75 and $14,050.50, as the task expects.

Send `Content-Type: application/json` with every request. Without it, `curl -d` sends the body as a form, and the service answers 422 with the message `the request body must be JSON sent with Content-Type: application/json`.

**Make targets** and what they run, if you prefer to run the commands yourself:

| Target | Command |
|---|---|
| `make up` | `docker compose up --build -d --wait`, then `docker compose restart lb` |
| `make seed` | `docker compose exec -T db psql -U postgres -d firm_payments < scripts/seed.sql` |
| `make reset` | Truncates `payments`, `idempotency_keys` and `firms`, then runs the seed again |
| `make test` | `docker compose up -d --wait db`, then `uv run --locked pytest` |
| `make lint` | `uv run --locked ruff check .`, `ruff format --check .`, `mypy app scripts migrations` |
| `make check` | `make test` and `make lint` |
| `make load` | `uv run --locked python scripts/load.py --base-url http://localhost:8000 ...` (see [Load](#load)) |

Use `make up`, not a bare `docker compose up`, whenever you bring the stand up again. nginx resolves the replica addresses only when it starts. Recreated replicas get new addresses, and `make up` restarts the balancer after them.

`docker compose stop` stops the stand and keeps the data. `docker compose down -v` removes the containers and the database volume; the database lives in an anonymous volume, so after any `down` the next `make up` starts empty and needs `make seed`.

## Verify it

### Acceptance walk-through

This continues from [Run it](#run-it).

1. Send the same sample request three more times. Expected: `201`, `201`, then `422`. After three batches Pinecrest has 50,000.00 − 3 × 13,251.25 = 10,246.25, which is less than one batch:

   ```
   {"error":{"code":"insufficient_funds","message":"the payer's balance does not cover the batch total"}}
   ```

2. Run `make reset` to return to the three seeded firms. Then send the request from page 3 of the task:

   ```bash
   curl -i -X POST http://localhost:8000/bulk-payments \
     -H 'Content-Type: application/json' \
     --data-binary @format-example-request.json
   ```

   Expected: `201`, with balances 4849925, 170075 and 230000 ($48,499.25, $1,700.75 and $2,300.00).

3. Idempotency. Run `make reset`, then send the sample twice with the same key:

   ```bash
   curl -i -X POST http://localhost:8000/bulk-payments \
     -H 'Content-Type: application/json' -H 'Idempotency-Key: demo-1' \
     --data-binary @sample-request.json
   ```

   - Both responses are `201` with identical bodies.
   - The second response carries `Idempotent-Replayed: true` and may come from another replica (see `X-Upstream`).
   - The money moved once.
   - The same key with a different body gives `422 idempotency_key_reuse`:

     ```bash
     curl -s -X POST http://localhost:8000/bulk-payments \
       -H 'Content-Type: application/json' -H 'Idempotency-Key: demo-1' \
       --data-binary @format-example-request.json
     ```

4. Load balancing: count the replicas that answer 30 health checks. Expected: three lines.

   ```bash
   for i in $(seq 30); do
     curl -s -o /dev/null -D - http://localhost:8000/healthz | grep -i '^x-upstream'
   done | sort | uniq -c
   ```

5. A replica fails while a client retries (check S3). Stop one replica, send the sample five times with one key, then bring the replica back:

   ```bash
   make reset
   docker stop $(docker compose ps -q app | head -1)
   for i in 1 2 3 4 5; do
     curl -s -o /dev/null -D - -X POST http://localhost:8000/bulk-payments \
       -H 'Content-Type: application/json' -H 'Idempotency-Key: s3' \
       --data-binary @sample-request.json | grep -i -e '^HTTP' -e '^idempotent-replayed'
   done
   make up
   ```

   Expected:
   - five `201` responses, four of them with `idempotent-replayed: true`;
   - Pinecrest at 3674875: the money moved once.

   On Podman one of the five requests can take about 2 s while nginx gives up on the stopped replica and passes the request on.

6. Run `make check`. Expected: exit code 0, with 159 tests passing and ruff and mypy clean.

### Tests

The tests run against a real PostgreSQL in the stand's `db` container, in a separate database called `firm_payments_test`. SQLite is never used, because lock behaviour is the thing under test. The schema comes from `alembic upgrade head`, and every test starts from the three seeded firms.

| Group | Tests | What they show |
|---|---|---|
| `tests/unit` | 64 | Amount parsing (U1–U3): no float, full match of `[0-9]+(\.[0-9]{1,2})?`, the 32-bit bound. Also the request hash, the request schema, the command builder, and engine construction. |
| `tests/db` | 14 | Migrations apply to an empty database and roll back. The schema matches the spec. The app refuses to start when the database is not at the migration head. Payment descriptions never reach error logs. |
| `tests/api` | 63 | The HTTP contract. A1–A16 cover both samples from the task, every rejection code, the error format and the payment order. I1–I4 and I6–I9 cover idempotency. |
| `tests/concurrency` | 18 | Races and locks; listed below. |

The concurrency tests do not rely on lucky thread scheduling. Threads start together through a `threading.Barrier`, the race scenarios run for many rounds, and invariants I-1 (no negative balance) and I-2 (the sum of balances is unchanged) are checked after each round. Waits are detected by polling `pg_stat_activity`, not by sleeping.

| Test | Scenario |
|---|---|
| C1 | 10 batches from one payer in parallel. The balance covers 3 of them: exactly three are created, seven are rejected with `insufficient_funds`. |
| C2 | 20 rounds of 50 crossing payments, A→B and B→A: no deadlock, no 503. |
| C3 | A cycle A→B→C→A plus a random mix: 200 batches on 16 threads with a fixed seed. |
| C4 | An external connection holds a firm's row lock. The request waits, then sees the committed balance. |
| C5, C8 | A lock or a key row is held longer than `lock_timeout`: 503 `busy`, nothing changed. |
| C6 | Many payers, one payee: the payee's balance equals the start plus all credits. |
| C7 | C1, I5 and crossing payments over HTTP against `uvicorn --workers 4`: separate processes that share only the database. |
| C9 | No free pool connection: 503 `busy`. |
| I5 | 8 simultaneous requests with one key: one execution, 7 replays with identical bodies (byte-identical over HTTP in C7). |

A statement-order test pins the SQL statements of spec section 8.2, in order.

**Mutation check.** A concurrency test that stays green when the lock is removed proves nothing. So the check breaks the service three ways, one at a time, and ran the concurrency tests against each version:

| Mutation | Behavioural tests that failed |
|---|---|
| M1: no `FOR NO KEY UPDATE` | C1, C2, C3, C4, C6 (too many batches accepted, lost updates, the sum of balances changed, a stale balance read) |
| M2: no `ORDER BY id` | C3, with real deadlocks, in 3 of 3 runs |
| M3: payer and payees locked in two queries | C2 and C3, with deadlocks |

Details and log excerpts are in `docs/verification-notes.md`.

### Static checks

`make lint` runs three checks:

- `ruff check`, including pydocstyle: every class, function and method in `app/`, `scripts/` and `migrations/` has a docstring.
- `ruff format --check`.
- `mypy --strict` over `app`, `scripts` and `migrations`, so every signature is annotated. `Any` appears explicitly in one place only: the JSON body read back from `idempotency_keys`.

Tests are exempt from the docstring rule. Their shared fixtures and helpers, and the largest tests, have docstrings and annotations anyway. CI runs `make check` on every push.

### Load

`scripts/load.py` creates 1,100 firms of its own, with ids from 1000, and sends batches through the balancer. After the run it checks that no balance is negative, that the sum of balances is unchanged, that every load firm's balance matches its payments, and that no response was a 500. It exits non-zero if any check fails. Run `make reset` afterwards: the script deletes only its own firms (ids 1000 and up), and only before a run.

```bash
make load ARGS="--scenario hot-payer --requests 2000 --concurrency 32"
make reset
```

Measured on 2026-11-24 on an Apple M4 Pro, with the stand in a Podman VM (6 CPUs, 3.7 GiB). The client ran inside the Compose network, so the figures do not include the host-to-VM port forwarding that `make load` now goes through.

| Scenario | Requests / concurrency | Requests/s | p50 ms | p95 ms | p99 ms | Statuses |
|---|---|---|---|---|---|---|
| uniform | 2000 / 32 | 1036.5 | 30.5 | 54.3 | 62.7 | 201 × 2000 |
| hot-payer (one payer) | 2000 / 32 | 619.0 | 37.0 | 123.0 | 159.9 | 201 × 2000 |
| hot-payee (one payee) | 2000 / 32 | 627.4 | 35.0 | 123.5 | 157.8 | 201 × 2000 |
| max-batch (1,000 payments, one payer) | 40 / 4 | 19.5 | 193.2 | 239.4 | 257.6 | 201 × 40 |
| max-batch, no contention | 40 / 1 | 18.1 | 50.2 | 69.6 | 69.9 | 201 × 40 |

In every run all invariant checks passed, there were no 503 or 500 responses, and requests split evenly over the three replicas. One payer sustained about 600 batches per second.

### The stand

| Check | Result |
|---|---|
| S1: `docker compose up -d --wait`, then `ps -a` | `db` and `lb` running, three `app` replicas healthy, `migrate` exited 0 |
| S2: 30 health checks through the balancer | Three distinct `X-Upstream` values |
| S3: stop one replica, then send the sample five times with one key | Five 201s, four of them replays. Balances changed once. |

**Where it was verified:**

- Locally, S1–S3 ran on Podman 5.8.2 with Docker Compose v5.1.3; no workaround was needed.
- On Docker Engine, the CI job `stand` runs `make up`, then `make seed`, posts the sample (201, Pinecrest 3674875) and checks for three upstreams. It runs on GitHub-hosted Ubuntu runners on every push.
- The CI job `check` runs `make check`.

## API

```
POST /bulk-payments
Content-Type: application/json
Idempotency-Key: <optional, 1 to 255 visible ASCII characters>
```

The body is exactly as in the task: `payer_firm_uuid` plus a list of `payments` with `amount`, `payee_firm_uuid` and `description`. The task leaves the path open; `/bulk-payments` is my choice.

The 201 body echoes the payments in request order, with their new ids and normalised amounts. It does not include balances, because the request is not authenticated. `GET /healthz` checks the database connection.

All errors share one format: `{"error": {"code": "...", "message": "..."}}`. Validation errors add `details` (`loc`, `msg`, `type`), which never echo the rejected value. The task asks for 422 when a request is denied and does not distinguish reasons. So every rejection caused by the request is a 422, and `code` says why:

| Status | `code` | When |
|---|---|---|
| 422 | `validation_error` | The body is not a JSON object, or a field breaks a rule: amount format, UUIDs, a missing `description`, an unknown field, more than 1,000 payments, a description longer than 500 characters, the key format |
| 422 | `self_payment` | A payment from the payer to itself |
| 422 | `firm_not_found` | The payer or a payee does not exist |
| 422 | `insufficient_funds` | The payer's balance does not cover the batch total |
| 422 | `balance_overflow` | A payee's balance would exceed 2,147,483,647 cents |
| 422 | `idempotency_key_reuse` | A key already used with a different request |
| 503 | `busy` | A lock wait or a pool wait timed out; `Retry-After: 1` |
| 500 | `internal_error` | Anything unexpected; details only in the logs |
| 404, 405, other | `not_found`, `method_not_allowed`, `http_error` | Routing errors, in the same format |

## Solution and approach

### Data model

The schema is created by Alembic migrations (`migrations/versions/0001_initial.py`), which the one-shot `migrate` service runs before any replica starts. The tables `firms` and `payments` have exactly the columns and types the task gives. I added only constraints:

- a unique index on `firms.uuid`, because every request looks firms up by it under lock;
- foreign keys from `payments` to `firms`, because the task describes the columns as ids of `firms` rows.

The third table, `idempotency_keys`, belongs to the idempotency extension described below. The service writes to it only when a request carries an `Idempotency-Key` header, so a request made exactly as the task describes touches only the two given tables.

Before it starts serving, each replica compares the revision in the database's `alembic_version` table with the head revision of the migrations it ships with. If they differ, the replica logs both revisions at ERROR and exits, so it never becomes healthy. A replica therefore never runs against a schema it was not built for: for example, when migrations failed, or when a newer image meets an older database. Verified on a separate stand: with `alembic_version` set to a bogus value, all three replicas exited with code 3; with the revision restored, they came up healthy. The check needs the database at startup, and the replicas have no restart policy. A replica started while the database is down exits and has to be started again with `make up`.

### The core: one transaction, ordered row locks

Each request is one transaction at the default isolation level (Read Committed), so a batch is applied completely or not at all. Parsing, validation, conversion to cents, the per-payee sums and the request hash are all done before the transaction opens. Inside it there are only database statements:

1. `SELECT set_config('lock_timeout', '5000ms', true)`.
2. Only when an `Idempotency-Key` was sent: claim the key (see below).
3. One query finds and locks every firm in the batch:

   ```sql
   SELECT ... FROM firms WHERE uuid IN (...) ORDER BY id FOR NO KEY UPDATE
   ```

4. Checks under the lock: every firm exists, the payer's balance covers the total, and no payee overflows.
5. Insert the payments, update the balances, store the response if a key was sent, then `COMMIT`.

Any rejection rolls back everything, including the key row.

**Why the database coordinates.** The task says the server runs as several load-balanced instances. Two requests for the same payer can therefore land on different processes, and no in-process lock or cache can protect a balance. The row lock in PostgreSQL is the one place every instance shares.

**Why not read, check in code, then write.** Two parallel batches from one payer would both read the same balance, both pass the check, and together drive it negative. With the row locked, the second batch waits and then sees the reduced balance. Test C1 shows exactly this, and mutation M1 shows the failure when the lock is removed.

**Why one query, ordered by `id`.** Crossing payments (A pays B while B pays A) would deadlock if each transaction locked its own payer first. Every transaction takes all its locks in one query in the same global order, so no wait cycle can form; PostgreSQL's documentation recommends exactly this protection against deadlocks. A design probe on PostgreSQL 16 gave 0 deadlocks in 2,400 crossing transactions with `ORDER BY` and 80 without it.

In this project on PostgreSQL 18.6, the two-firm crossing test did *not* deadlock without `ORDER BY`. The three-firm cycle test did, in every run. So the claim rests on C3 and on mutations M2 and M3, not on the two-firm case. Why the two-firm case stays clean here is not established; the notes list the candidate explanations.

**Why `FOR NO KEY UPDATE`, not `FOR UPDATE`.** The balance is not a key column, and a plain `UPDATE` of it takes this same lock mode. `FOR UPDATE` would also block the foreign-key check (`FOR KEY SHARE`) of another transaction inserting a payment that references the same firm. `FOR NO KEY UPDATE` does not.

**Why not `SERIALIZABLE`.** It needs a retry loop for serialization failures (40001), a retry limit and an answer for when that limit runs out. Under contention on one firm the retries multiply. Locks make the waiting explicit and deterministic to test.

**Why not a conditional `UPDATE ... WHERE balance_cents >= total`.** It still has to update payees in a global order to avoid deadlocks. The payer check can then run after some credits have already been applied, and it cannot tell the rejection reasons apart.

**Bounded waiting.** `lock_timeout` is 5 s. When it expires (SQLSTATE 55P03), the client gets `503 busy` with `Retry-After: 1` rather than an indefinite wait. A deadlock (40P01) or an exhausted connection pool also maps to 503. A deadlock should never happen by construction, so it is also logged at ERROR. The timeout is set with `set_config(..., true)` because `SET LOCAL` does not accept a bound parameter; a design probe showed it fails with a syntax error.

### Money

Amounts are strings in the task and stay strings until they are converted to integer cents. The string must fully match `[0-9]+(\.[0-9]{1,2})?` (`fullmatch`, so `"1\n"` fails), be at most 11 characters and be greater than zero. The character class is `[0-9]`, not `\d`, because Python's `\d` also matches digits from other scripts. A JSON number such as `12.5` is rejected: it has already passed through floating point. `float` is never used for money.

The task's tables use `INTEGER`, so every amount, batch total and balance must fit in 2,147,483,647 cents (about $21.4 million). A batch that would push a payee past that bound is rejected with `balance_overflow`, not a database error.

### Synchronous FastAPI

The endpoint is a plain `def`, and the database access is synchronous SQLAlchemy 2.1 with psycopg 3. There are no external calls, so request time is database time, and the connection pool bounds concurrency in any case. Async would add complexity with no gain.

FastAPI runs sync endpoints in AnyIO's thread pool, which allows 40 threads by default. At startup the app sets that limit to the pool size (10), so a request waits for a thread instead of failing at pool checkout. The pool has no overflow and checks each connection before handing it out. The connection budget is 3 replicas × 10 = 30 connections, against PostgreSQL's default of 100.

### Idempotency-Key: a feature beyond the task

The task does not ask for idempotency. I added it as an extra feature, implemented beyond what was expected, for one purpose: **to rule out duplicate operations**, so that a retried request never moves the same money twice.

**Why add it.** Clients of a payment API retry: after a timeout, a dropped connection, or a crash between the commit and the response. Suppose a client's request commits but the response is lost. The client cannot tell whether the payments went through, and a plain retry creates a second batch of payments. The server cannot deduplicate on its own either: two identical batches in a row are a legitimate operation, so only the client knows that a request is a retry. An idempotency key lets the client say so, and the server then guarantees at most one execution per key.

The header is optional. Without it the service behaves exactly as the task describes and touches only the two given tables.

**How it works:**

- The key is stored in a service table, `idempotency_keys`, keyed by (payer, key). It is written in the *same* transaction as the payments, so the key and the money commit together or not at all.
- The key row is inserted first, with `INSERT ... ON CONFLICT DO NOTHING`. A second request with the same key waits on the unique index until the first one finishes.
  - If the first commits, the second returns the stored response with `Idempotent-Replayed: true`.
  - If the first rolls back, the second runs itself.
  
  No "in progress" state and no 409 are needed. A design probe confirmed this behaviour, and test I5 re-confirms it.
- Only a 201 is stored. A rejection rolls back the key row too, so after a top-up the same key can succeed.
- The table also holds a SHA-256 hash of the canonical request. The same key with a different request is rejected with 422, as the IETF draft recommends. `"300"` and `"300.00"` hash the same.
- The response body is stored as `JSON`, not `JSONB`. `JSONB` reorders object keys, while `JSON` returns the text unchanged, so a replay is byte-identical to the original.
- Keys are scoped per payer, since there is no authentication to scope them by.

The header follows the IETF draft `draft-ietf-httpapi-idempotency-key-header`. It is an expired draft, not a standard.

### The stand: three replicas behind nginx

`docker-compose.yml` runs these services:

| Service | Role |
|---|---|
| `db` | PostgreSQL 18 |
| `migrate` | A one-shot service that runs `alembic upgrade head`; the replicas wait for it, so three replicas never race on migrations |
| `app` | Three replicas, one Uvicorn process each |
| `lb` | nginx |

All coordination lives in the database, so any replica can serve any request and the routing does not matter. nginx adds an `X-Upstream` header naming the replica that answered, so the distribution can be seen.

**Why three replicas.** Correctness does not depend on the number, but three is the smallest count that gives all of these at once:

- A lock cycle needs three parties, and each can be served by a different instance.
- With one replica stopped, there are still several instances (check S3).
- The stand fits the connection budget.

**nginx and payments.** nginx never sends a `POST` to a second replica once it has been sent to one, so the balancer cannot duplicate a payment by itself. A request it could not deliver at all, such as a refused or timed-out connect, goes to the next replica. In S3 that took 2 s against the stopped replica. Retrying after a lost response is the client's job, with an idempotency key.

The balancer accepts bodies up to 8 MB. nginx's 1 MB default is below valid requests: 1,000 payments with 500-character descriptions take about 1.1 MB with two-byte characters, and up to about 6.1 MB when every character is sent as a `\u` escape.

### The 1,000-payment batch limit

The task sets no limit, but without one a single huge batch could hold locks on thousands of firms for seconds. The limit follows a criterion rather than taste: **the largest batch should hold its locks for at most about 5% of `lock_timeout`**, that is 250 ms of 5 s. That way about twenty maximal batches can queue on one firm before any of them times out.

A design probe measured lock-hold time at about 0.11 ms per payment:

| Payments | Locks held, ms |
|---|---|
| 1,000 | 109 |
| 2,000 | 206 |
| 10,000 | 1,067 |

In the project, through nginx, an uncontended 1,000-payment request takes 50 ms median and 70 ms at most, end to end. With four such batches from one payer in parallel, the median is 196 ms (`statistics.median`; the nearest-rank p50 in the load table is 193.2 ms). The maximum is 257.6 ms, about 7 ms over the budget. The difference is time spent queueing behind the other batches on the payer's row, not lock-hold time. The limit stays at 1,000: the implementation controller ruled so, and both numbers are in the notes. A client with more than 1,000 payments splits them into several requests, and each request is atomic on its own. Both limits are configurable: `MAX_PAYMENTS_PER_REQUEST` and `MAX_DESCRIPTION_LENGTH`.

### Observability

Each bulk payment request produces one structured JSON log record with:

- the request id, taken from nginx's `X-Request-Id`;
- the replica (its container host name);
- the payer;
- the number of payments;
- the total;
- the outcome: `created`, `replayed` or the rejection code;
- the duration.

Payment descriptions are never logged. The engine runs with `hide_parameters=True`, so not even a database error's traceback carries them, and a test checks this.

## Issues I ran into

**During design (throwaway probes, before any code was written):**

- `SET LOCAL lock_timeout = $1` is a syntax error, because `SET` does not take bound parameters. `set_config(..., true)` works.
- `JSONB` reordered the keys of the stored response, so a replay no longer matched the original byte for byte. The column is `JSON` instead.
- An exhausted SQLAlchemy pool raises `sqlalchemy.exc.TimeoutError`, which is not a `DBAPIError`. It needed its own mapping to 503.
- PostgreSQL rejects U+0000 in `TEXT`. Without a validation rule this surfaced as a 500.

**Other design-time choices:**

- PostgreSQL 19 was released on 2026-11-19, a few days before this work. I stayed on 18: a major release a few days old is not where a payment service should start, and nothing here needs 19.
- Docker was not available where the design probes ran, so the stand was first verified during implementation (see above).

**During implementation:**

- **Connection pools per process.** `functools.lru_cache` does not make a function run once. When a worker's first requests arrived together, each built its own engine and pool, so one process could hold up to about twice `DB_POOL_SIZE` connections. The C7 workers held 41 connections against a budget of 40. The engine is now built once, under a lock, at startup. A test covers it.
- **Descriptions in logs.** A database error's traceback includes SQLAlchemy's statement parameters, and those contain payment descriptions. The fix is `hide_parameters=True` and logging only the driver error.
- **Inputs that caused a 500 or leaked into a response.**
  - A JSON escape such as `"\ud800"` (a lone surrogate) in a description cannot be encoded for the request hash or the database, and would have ended in a 500. It is now rejected with a 422, and a test covers it.
  - Pydantic's UUID error message quoted the rejected input. It is replaced with fixed text.
  - A body that is not valid UTF-8 was a 400 outside the error format. It is now a 422.
- **Stale balancer.** After the replicas were recreated, nginx still pointed at the old addresses and returned 504. `make up` now restarts it.
- **The 1 MB body limit.** nginx's default rejected a valid 1,000-payment batch with an HTML 413, so the balancer's limit is now 8 MB.
- **`curl -d` without `Content-Type`.** This got a 422 whose message did not say why. It now says to send JSON with `Content-Type: application/json`.
- **The 80 deadlocks did not reproduce.** The design probe's two-firm result did not repeat on PostgreSQL 18.6 (see the core section). The first write-up of this in the notes guessed a cause from a query plan it had not captured; the published notes give the captured plans and say the cause is not established.
- **Podman.** A stopped replica's address timed out instead of refusing the connection, so nginx waited 2 s (`proxy_connect_timeout`) before moving on.

## Assumptions

The task does not specify these; `docs/spec.md` section 4 has the reasoning for each.

1. Requests are not authenticated; `payer_firm_uuid` is taken from the body as is.
2. `firms.uuid` is stored in lowercase canonical form; incoming UUIDs are normalised to it.
3. Balances and amounts fit in 32-bit `INTEGER`, as in the task's tables; exceeding the bound is a 422.
4. Other parts of the platform may legitimately allow a negative balance, so the database has no constraint `CHECK (balance_cents >= 0)` on `firms`. This service still never drives a balance below zero: it checks the payer's balance in code, under the row lock (`app/service.py`).
5. A payment from a firm to itself is rejected.
6. Two identical requests without a key are two legitimate batches, not a duplicate.
7. Payments are initiated by people, so the rate per firm is modest. The load run measured about 600 batches per second through one firm.
8. `description` is required, as in all of the task's examples.
9. Unknown fields are rejected rather than ignored, because an ignored field could carry meaning, such as a currency.
10. The batch size and description length limits are safeguards the task does not mention. Both are configurable.
11. The endpoint path, `POST /bulk-payments`, is my choice.

## Known limitations and possible improvements

1. **No authentication.** Anyone who can reach the API can pay from any firm by naming its UUID. This is the first thing a real deployment needs; the payer would come from the session. The stand binds to `127.0.0.1` for that reason.
2. A request without an idempotency key is not protected against retries. Improvement: make the key mandatory.
3. Keys are kept forever. Improvement: publish a retention period of at least 24 hours, so that client retries stay safe, and delete older keys by `created_at`.
4. `INTEGER` limits balances to about $21.4 million. Improvement: `BIGINT`, which changes the given schema.
5. A hot payer's batches run strictly one after another. A hot payee queues its payers. Improvement: a journal of credits with deferred balance recalculation.
6. The database has no `CHECK (balance_cents >= 0)` constraint as a last line of defence. The balance check in code is the only guard, so a bug in it would not be stopped by the database (assumption 4). Improvement: add the constraint once it is clear that no other part of the platform needs negative balances.
7. Payments have no creation time and no batch id.
8. There are no metrics or tracing beyond the structured logs.
9. Connections grow with the number of instances. Improvement: a database-side pool such as PgBouncer.
10. If `Idempotency-Key` is sent twice, the first value wins. Improvement: reject such a request.
11. The stand is not highly available: one nginx and one PostgreSQL. nginx resolves the replica addresses only at start, hence `make up`. `deploy.replicas` needs Compose v2; the legacy `docker-compose` v1 ignores it.
12. More than 1,000 payments must be split into several requests, which are atomic one by one, not together.
13. `lock_timeout` bounds each lock wait, not the whole request. A request that waits on several rows in turn could run past nginx's 60 s read timeout and get an HTML 504 while its transaction still commits. Improvement: a request deadline, or a transaction-scoped `statement_timeout`.
14. A body over 8 MB gets nginx's own 413 page instead of the JSON error format.
15. All performance figures come from one machine. They are measurements, not capacity guarantees.

## How I worked

I used Claude models throughout. The implementation, the reviews and this README ran in **Claude Code** (CLI); the index in `ai-logs/` names the tool and model of every session. Four models, each chosen per step (`docs/agent-workflow.md`):

| Step | Model | What it produced |
|---|---|---|
| Design and specification | Claude Fable 5.2 | `docs/spec.md`, with throwaway probes against PostgreSQL that confirmed the locking and idempotency behaviour |
| Implementation plan | Claude Fable 5.2 | `docs/plan.md`, nine tasks |
| Validation of the docs against the task, then control of the implementation | Claude Opus 5.5 | Spec revision 4; one controller session running the plan |
| Scaffold: project, Docker, schema (tasks 1–2) | Claude Sonnet 5.5 subagents | |
| Core: validation, transaction, idempotency (tasks 3–6) | Claude Opus 5.5 subagents | |
| Concurrency tests, mutation check, load, stand checks (tasks 7–8) | Claude Opus 5.5 subagents | |
| Review after every task and every fix round | Mostly Claude Opus 5.5; Sonnet 5.5, and Haiku 5.5 once, for small re-reviews | Findings fixed until each task's review was clean |
| Whole-branch review | Claude Fable 5.2 | Three important findings and two minor ones, fixed in one wave by an Opus subagent and confirmed by a Sonnet re-review |
| Typing and docstrings, schema check at startup | Claude Opus 5.5 subagent, at my request | |
| This README | Claude Opus 5.5, at my request | Drafted from the spec, the verification notes, the code and my outline, then fact-checked against the repository by a separate Opus subagent |

The implementation ran under the `subagent-driven-development` skill from the superpowers plugin:

- a fresh implementer subagent per task;
- a review of spec compliance and code quality after each task;
- fix rounds until the review was clean;
- a final review of the whole branch on the strongest model.

When something was ambiguous, the controller did not stop and wait. It made a ruling, wrote it down and reported it.

Every commit carries a `Co-Authored-By` trailer naming the model of the session that made it. The design documents were written in the Fable design sessions but committed from the Opus controller session (fce9a5b), so that commit names Opus.

**Logs.** `ai-logs/` holds the working sessions, with an index of their tool, model and role. They differ from the raw export in two ways:

- The account email and the home directory path are masked. Nothing else was edited.
- Two aborted early attempts at starting the implementation are left out.

**Whose decisions.** I made the decisions marked **[R]** in the spec:

- the optional idempotency key, stored in the same database rather than in a queue or Redis;
- synchronous code;
- ordered row locks instead of `SERIALIZABLE` or conditional updates;
- storing only 201 responses;
- the three-replica stand;
- the 1,000-payment limit and its criterion.

Some of them began as the model's proposals, which I checked and accepted. These are `FOR NO KEY UPDATE` instead of `FOR UPDATE`, `JSON` instead of `JSONB` for the stored response, and storing only 201 responses.

The spec marks with **[P]** the proposals made during design that I did not decide separately. Examples are the single error format with 422 for every rejection, the key format, the libraries, and the default limits other than the batch size.

During implementation the controller made twenty rulings on its own and recorded them in its session log. The ones that changed the result:

- fixing the engine race in the service code;
- keeping the 1,000 limit after the contended run went over the budget;
- the routing error codes;
- `make up` restarting the balancer;
- `make load` leaving the reset to the operator.

These were my own requests:

- moving `make test`, `lint` and `load` to uv on the host;
- the schema check at startup;
- full typing and docstrings;
- the README draft.

`docs/plan.md` (Execution Record) lists what changed against the plan.

## Repository layout

```
app/            main.py (app, lifespan, error handlers, logging), api.py (HTTP <-> service),
                service.py (the transaction), idempotency.py, schemas.py (request,
                amount parsing), models.py, db.py, schema_revision.py (startup
                check against the migration head), config.py, errors.py
migrations/     Alembic; one revision with the three tables
tests/          unit/, db/, api/, concurrency/
scripts/        seed.sql, init-test-db.sql, load.py
deploy/         nginx.conf
docs/           spec.md, plan.md, agent-workflow.md, verification-notes.md
ai-logs/        AI session logs
sample-request.json          the request from page 4 of the task
format-example-request.json  the request from page 3 of the task
docker-compose.yml, Dockerfile, Makefile, pyproject.toml, uv.lock
```

## Two questions

**How much time did you spend on this task?**

- [ ] Less than 3h
- [X] 3h to 6h
- [ ] More than 6h (how long?)

**How proud are you of your work?**

- [ ] Very proud
- [X] Fairly proud
- [ ] Good enough
- [ ] Somewhat disappointed
