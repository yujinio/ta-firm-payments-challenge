# Firm Payments: Bulk Payment Service Specification

- Date: 2026-11-21
- Status: approved; revision 5 (2026-11-24). Revision 2 folded in the rules found while planning and the probe results of section 17. Revision 3 adds the run stand (section 5.6) and the justified batch limit (section 5.7). Revision 4 follows a validation against the task statement: the second request from the task (page 3) becomes a sample, the rationale for the service table is restated, the Python version is pinned, and the local container runtime is recorded. Revision 5 records what the implementation and its checks found: the observed mutation results (sections 5.4, 8.3, 13.7), the measured batch limit (5.7), the stand checks on Podman and Docker Engine (13.8, 17), configurable host ports and the balancer's body limit (5.6), the routing error codes (8.1) and the per-wait meaning of `lock_timeout` (9.3, 15). Measured results are in `docs/verification-notes.md`
- Location in repository: `docs/spec.md`
- Basis: TaxDome technical challenge "Firm Payments: Build a bulk payment service" (PDF, 5 pages)

Markers used in the text:

- **[R]**: decision made by the author during the design discussion;
- **[P]**: proposal introduced during design and not yet confirmed separately;
- **[A]**: assumption: the task does not specify this.

---

## 1. Purpose

The service accepts a bulk payment request from one firm to other firms on the platform. If the payer's balance covers the total of the request, the service atomically records the payments, debits the payer, credits the payees and responds with 201. If the balance is insufficient, it rejects the whole request and responds with 422.

The service runs as several instances behind a load balancer and uses a relational database.

## 2. Goals, non-goals, anti-goals

**Goals**

1. Money correctness: under any interleaving of requests, this service never drives a balance negative and money is neither created nor destroyed.
2. Batch atomicity: either everything is applied or nothing is.
3. Correctness across multiple instances: all coordination goes through the database; there is no in-process state.
4. Protection against duplicate execution when a client retries.
5. Verifiability: every property in goals 1–4 is confirmed by an automated test.
6. Reproducibility: clone, run with one command, send the example from the task.

**Non-goals**

- Authentication and authorization.
- Creating or modifying firms, topping up balances, reading payment history.
- Currencies other than US dollars.
- Cleanup of expired idempotency keys.
- Metrics or tracing beyond structured logs.

**Anti-goals** (what would make the solution a failure even if it works)

- Extra infrastructure (message broker, cache) where a database transaction is sufficient.
- Correctness that depends on luck in thread scheduling rather than on locks.
- Money arithmetic with floating point.
- Changing the columns of the given `firms` and `payments` tables.

## 3. Requirements from the task

| No. | Requirement | Source |
|---|---|---|
| R1 | Accept a JSON request for a batch of payments from a single firm | p. 2, 3 |
| R2 | If funds are insufficient for all payments, reject the whole request with 422 | p. 2 |
| R3 | Otherwise insert the payments, debit the payer, credit the payees, respond 201 | p. 2 |
| R4 | The server runs as several instances behind a load balancer | p. 2 |
| R5 | A relational database (PostgreSQL, MySQL or similar) | p. 2 |
| R6 | Amount: a string, dollars, at most two optional decimal places, always positive | p. 3 |
| R7 | Tables `firms` and `payments` with the given columns | p. 3 |
| R8 | The example from the task yields balances of $36,748.75 / $1,700.75 / $14,050.50 | p. 4 |
| R9 | One of Ruby, Python or Go (Python is our choice); commonly used libraries | p. 2 |
| R10 | The repository can be cloned, installed, run and sent the sample requests: both the request on page 3 and the one on page 4 | p. 3, 4, 5 |
| R11 | Commit history, AI conversation logs, README written as a pull request, two questions at the end | p. 5 |

## 4. Assumptions

1. **[A]** The request is not authenticated: `payer_firm_uuid` from the body is accepted as is. In a real system the payer would be determined from the session.
2. **[A]** `firms.uuid` is stored in lowercase canonical form, as in the example. Incoming UUIDs are normalized to this form.
3. **[A]** Balances and amounts fit in a 32-bit `INTEGER` (up to 2,147,483,647 cents, about $21.4 million). Exceeding the bound is a 422, not a database error.
4. **[A]** A negative balance may be valid in other parts of the platform, so `CHECK (balance_cents >= 0)` is not added. The service itself never drives a balance below zero.
5. **[A]** A payment from a firm to itself is meaningless and is rejected.
6. **[A]** Two identical requests in a row without an idempotency key are a legitimate operation, not an error.
7. **[A]** Payments are initiated by people, so the request rate per firm is modest. The estimate "hundreds of batches per second through one firm" is calculated, not measured; the load scenario verifies it (section 13.5).
8. **[A]** Every payment carries a `description`, as in all of the task's examples; a payment without one is rejected.
9. **[A]** The attributes shown in the task are the complete request format. Unknown fields are rejected rather than ignored, because an ignored field could carry meaning (a currency, for example).
10. **[A]** The limits on batch size and description length (section 10) are safeguards the task does not mention. They are configurable; the batch limit is justified in section 5.7.
11. **[A]** The task does not name the endpoint path. `POST /bulk-payments` is our choice, and the README states it.

## 5. Considered approaches and the choice

### 5.1. Scope of the solution

| Option | Summary | Outcome |
|---|---|---|
| Strictly as specified | Two tables, request as written, idempotency only in the README | Rejected: a retry after a timeout creates a second batch of payments |
| **Additive extension** | Optional `Idempotency-Key` header and a service table; the given tables and examples work unchanged | **[R] Chosen** |
| Changing the given tables | `BIGINT`, `batch_id`, `created_at`, `CHECK` | Rejected: changes the columns and types the task gives for `firms` and `payments` |

Rationale [R]: the task does not forbid extension, and without a key duplicates cannot be prevented. The task describes the platform's database as two tables; the chosen option keeps both exactly as given and adds a service table, `idempotency_keys`, next to them. The service writes to it only when a request carries the `Idempotency-Key` header, so a request made strictly as the task describes touches only the two given tables.

### 5.2. Where to store the idempotency key

| Option | Outcome |
|---|---|
| **Table in the same database, written in the same transaction** | **[R] Chosen**: writing the key and the payments is atomic |
| Redis | Rejected: the key lives outside the database transaction; if a failure occurs between the commit and the key write, a retry runs as a new request |
| Message broker | Rejected: it does not remove duplicates (delivery is at-least-once); deduplication moves to the consumer and again needs a unique key in the database. Internal asynchrony is not forbidden by a synchronous response, but it is over-engineering |
| Deduplication by content hash | Rejected: identical batches in a row are legitimate (assumption 6) |

### 5.3. What to store under the key

| Option | Outcome |
|---|---|
| **Only the successful 201 response** | **[R] Chosen**: a rejection rolls back the whole transaction and leaves no side effects, so a retry is checked anew |
| Any business result, including 422 (Stripe API v1 model) | Rejected: stricter, but a rejection would need a separate commit path, and the client would need a new key after topping up |

Guarantee: each key corresponds to at most one execution.

### 5.4. Protecting balances from races

| Option | Outcome |
|---|---|
| **Ordered row locks: one query locks all firms in the batch in `id` order, balance check in code** | **[R] Chosen** |
| Conditional `UPDATE ... WHERE balance_cents >= :total` without a read | Rejected: crossing payments still block each other; updates strictly in `id` order fix this, but the payer check then may run after some credits have been applied |
| `SERIALIZABLE` with retries | Rejected: requires a retry loop, a limit and a response when the limit is exhausted; under contention for one firm retries multiply; MySQL implements this level differently |
| Locking only the payer, advisory lock on the payer | Rejected: a credit still locks the payee's row, and crossing payments hang |
| In-memory process locks | Rejected: they do not work across instances |
| Balance as a sum over a journal | Rejected: contradicts the `balance_cents` column |

Rationale [R]: default isolation level, no retries, the check is readable in code and deterministically testable. A single acquisition order is the protection against deadlocks recommended by PostgreSQL documentation. A probe confirmed the choice (section 17): 2,400 crossing transactions with the ordered query produced no deadlocks, and the same run without `ORDER BY` produced 80. In the project (PostgreSQL 18.6) the two-firm crossing test C2 did not deadlock without `ORDER BY`; the three-firm cycle test C3 did, in every run (section 13.7). Cost: payee rows are locked for the whole transaction; the transaction is short, and this is accepted (section 9.3).

### 5.5. Synchronous or asynchronous database access

| Option | Outcome |
|---|---|
| **Synchronous SQLAlchemy session, endpoint declared as `def`** | **[R] Chosen** |
| Asynchronous session | Rejected: there are no external calls, request time is determined by the database, and concurrent transactions are bounded by the connection pool; the code and tests are more complex with no gain |

### 5.6. How the instances are run

| Option | Outcome |
|---|---|
| One container, Uvicorn with several worker processes | Rejected: meets the requirement in spirit only; there is no balancer, and the migration race stays hidden |
| **Compose stand: three application replicas behind an nginx balancer, migrations as a one-shot service** | **[R] Chosen** |
| Both modes, the stand as an optional profile | Rejected: two ways to run the project in the README |

Rationale [R]: the task states that the server runs as multiple load-balanced instances, and the reviewers assess how the solution would fit their infrastructure. `docker compose up` starts exactly that arrangement, and the acceptance and load runs go through the balancer.

**Why three replicas.** Correctness does not depend on the number. Three is the smallest count that gives three properties at once:

1. A lock-order cycle needs three parties (A→B, B→C, C→A, test C3). With three replicas each party can be served by a different instance; with two, some pair always shares one.
2. With one replica stopped, the remaining two are still multiple instances, so the stand keeps showing the requirement during a failure (check S3).
3. It fits the connection budget: 3 replicas × `DB_POOL_SIZE` 10 = 30 connections against PostgreSQL's default limit of 100, which leaves room for the tests, the migration and manual `psql` sessions.

The number is a property of the stand, not a capacity recommendation.

**Consequences of the stand:**

- Migrations run once, in a separate `migrate` service that the replicas wait for. Three replicas each running `alembic upgrade head` at start would race.
- Each replica is a single Uvicorn process. Replicas publish no host port. The balancer publishes `127.0.0.1:${LB_PORT:-8000}`; `db` publishes `127.0.0.1:${DB_PORT:-5432}` so that tests and the multi-process test server can run on the host. Both bind to the loopback interface: the API has no authentication. The variables let a machine that already runs PostgreSQL or uses port 8000 choose other ports.
- The balancer accepts bodies up to 8 MB (`client_max_body_size`); nginx's 1 MB default is below the largest valid request (1,000 payments with 500-character non-ASCII descriptions, about 1.1 MB). It forwards the client's `Host` header, so redirects keep the published port.
- nginx does not pass a `POST` to another replica once it has been sent to one, so the balancer never duplicates a payment by itself. A request that could not be delivered at all is passed to the next replica. Retrying after a lost response stays the client's job, with an idempotency key.
- nginx resolves the replica addresses when it starts. After replicas are recreated or rescaled, the balancer must be restarted.
- The balancer adds the response header `X-Upstream` with the address of the replica that served the request, so the distribution can be observed (check S2).

### 5.7. The batch size limit

| Option | Outcome |
|---|---|
| No limit | Rejected: one huge batch holds locks on thousands of firms for seconds |
| **1,000 payments per request, configurable** | **[R] Chosen** |
| A much higher limit (10,000 or more) | Rejected: see the measurement below |

The limit is artificial: the task defines none. It is set by a criterion rather than by taste: **the largest allowed batch should hold its locks for no more than about 5% of `LOCK_TIMEOUT_MS`** (250 ms of 5,000 ms), so that about twenty maximal batches can queue on one firm before any of them times out.

Measured in a throwaway probe (section 17): one machine, local connection, the planned ORM approach, median of 7 runs.

| Payments in the batch | Firm rows locked | Locks held, ms | Whole transaction, ms |
|---|---|---|---|
| 1 | 2 | 3 | 5 |
| 100 | 101 | 13 | 19 |
| 1,000 | 1,001 | 109 | 126 |
| 2,000 | 2,001 | 206 | 236 |
| 5,000 | 5,001 | 484 | 581 |
| 10,000 | 10,001 | 1,067 | 1,178 |

Growth is linear, about 0.11 ms per payment. At 1,000 payments the hold is 109 ms, and the whole transaction took 172 ms in the worst of the seven runs: inside the 250 ms budget, with a margin for network latency. At 2,000 the margin is gone. At 10,000 a single batch holds 10,001 rows for over a second, so the fifth queued batch would time out.

These figures come from one machine and are not a guarantee. Load scenario L1 repeats the measurement in the project, through the balancer, and records the result.

Measured in the project (`docs/verification-notes.md`, through nginx, one Podman VM with 6 CPUs): without contention a 1,000-payment request took 50.3 ms median and 69.9 ms at most end to end, so the locks are held for at most that long, about a quarter of the budget. With four such requests from one payer in parallel, the median was 196.0 ms and the maximum 257.6 ms: each request also waits behind the others on the payer's row. The limit stays at 1,000.

A consequence to state in the README: a client with more than 1,000 payments splits them into several requests, and each request is atomic on its own, not all of them together.

## 6. Stack

| Component | Choice | Status |
|---|---|---|
| Language | Python 3.14, pinned in `.python-version` and in the Docker base image so that host runs and the image use the same minor version. 3.15 is too recent to rely on wheels for every dependency | [R] |
| Web | FastAPI, Uvicorn | [R] |
| Validation | Pydantic v2, pydantic-settings | [P] |
| ORM | SQLAlchemy 2.x, synchronous mode (2.1 is current as of 2026-11-21) | [R] |
| Driver | psycopg 3 | [P] |
| Migrations | Alembic | [P] |
| Database | PostgreSQL 18. Version 19.0 was released on 2026-11-19; a major release a few days old is not where a payment service should start, and nothing here needs its features | [P] |
| Tests | pytest, httpx2 (the HTTP client Starlette's test client requires) | [P] |
| Quality | ruff, mypy | [P] |
| Run | Docker Compose: PostgreSQL, a one-shot migration service, three application replicas, an nginx balancer (section 5.6) | [R] |
| Dependencies | uv with a lock file; the implementer may replace it with pip | [P] |

Versions are pinned by the lock file when the project is created. Specific version numbers are intentionally omitted here: they must be taken current, not from memory.

The solution uses two PostgreSQL-specific features: `INSERT ... ON CONFLICT DO NOTHING` and `FOR NO KEY UPDATE`. MySQL has direct equivalents (`INSERT IGNORE`, `FOR UPDATE`), but porting is out of scope.

## 7. Data model

### 7.1. Given tables

Columns and types are as in the task. Only constraints and an index are added.

```sql
CREATE TABLE firms (
    id            INTEGER PRIMARY KEY,
    name          TEXT    NOT NULL,
    balance_cents INTEGER NOT NULL,
    uuid          TEXT    NOT NULL,
    CONSTRAINT uq_firms_uuid UNIQUE (uuid)
);

CREATE TABLE payments (
    id            INTEGER GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
    payer_firm_id INTEGER NOT NULL REFERENCES firms (id),
    payee_firm_id INTEGER NOT NULL REFERENCES firms (id),
    amount_cents  INTEGER NOT NULL,
    description   TEXT    NOT NULL
);
```

Notes:

- `uq_firms_uuid` **[P]**: firms are looked up by `uuid`; without an index this is a full table scan under lock.
- Foreign keys **[P]**: the task describes the columns as "id of the firms row", so referential integrity is implied.
- `firms.id` has no auto-generation: the service does not create firms, and the example inserts explicit values.
- No indexes on `payments.payer_firm_id` and `payee_firm_id`: this service does not read by them.

### 7.2. Service table

```sql
CREATE TABLE idempotency_keys (
    id              BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    payer_firm_uuid TEXT        NOT NULL,
    key             TEXT        NOT NULL,
    request_hash    TEXT        NOT NULL,
    response_body   JSON,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT uq_idempotency_keys UNIQUE (payer_firm_uuid, key)
);
```

Notes:

- The key scope is the payer: there is no authentication and no other natural owner of the key.
- `payer_firm_uuid` instead of `payer_firm_id`, with no foreign key: the key is inserted as the first step, before firms are read (section 8.2). A row for a nonexistent payer would not survive, because the transaction rolls back.
- `response_body` is nullable because the row is inserted at the start of the transaction, while the response is known at the end. A committed row always has the value filled; test I7 verifies this.
- `response_body` is `JSON`, not `JSONB`. `JSONB` reorders object keys, while `JSON` returns the stored text unchanged, so a replayed response is byte-identical to the original. The column is never queried by content.
- The response status is not stored: only 201 is stored.
- `request_hash` is SHA-256 over the canonical form of the parsed request: payer and the ordered list of triples (payee, amount in cents, description). Requests with `"300"` and `"300.00"` are considered identical; payment order is significant.

## 8. Behavior

### 8.1. HTTP contract

**Request**

```
POST /bulk-payments
Content-Type: application/json
Idempotency-Key: <optional>
```

The body is as in the task.

**Validation rules**

| Field | Rule |
|---|---|
| Body | An object; unknown fields are forbidden [P] |
| `payer_firm_uuid` | A valid UUID in any form Pydantic accepts (hyphenated, bare hex, in braces), normalized to canonical lowercase |
| `payments` | An array of 1 to `MAX_PAYMENTS_PER_REQUEST` elements (default 1000 [R], section 5.7) |
| `amount` | Strictly a JSON string of at most 11 characters, full match of `[0-9]+(\.[0-9]{1,2})?`, value greater than zero, at most 2,147,483,647 cents |
| `payee_firm_uuid` | A valid UUID, normalized the same way, not equal to the payer |
| `description` | A string, required, no longer than `MAX_DESCRIPTION_LENGTH` (default 500 [P]), must not contain the character U+0000 |
| `Idempotency-Key` | 1 to 255 characters in the range 0x21–0x7E (visible ASCII, no spaces) [P]. If the header is sent more than once, the first value is used |
| Batch total | At most 2,147,483,647 cents |

Amount-parsing details that are easy to miss:

- The comparison is a full match (`re.fullmatch`); otherwise the string `"1\n"` would pass.
- The class is `[0-9]`, not `\d`: in Python, `\d` accepts digits from other scripts.
- A number in JSON (`12.5` without quotes) is rejected: it has already passed through floating point.
- Conversion to cents is integer arithmetic, via `Decimal` or by splitting the string. `float` is not used anywhere.
- A string longer than 11 characters is rejected before any numeric conversion: 8 integer digits, a point and 2 decimals is the longest value that fits the bound.

`description` rejects U+0000 because PostgreSQL `TEXT` cannot store it. Without the rule such a request would end in a 500 (confirmed, section 17).

**201 response**

```json
{
  "payer_firm_uuid": "3f1c9a2e-7b4d-4c1e-9a55-2d8e6f0b7c41",
  "total_amount": "13251.25",
  "payments": [
    {
      "id": 1,
      "payee_firm_uuid": "e5f18b3c-2a9d-4c07-8e6b-1d4a7f9c3b25",
      "amount": "6250.00",
      "description": "Overflow returns, August 2026"
    }
  ]
}
```

Payments are listed in request order. The balance is not included in the response: the request is not authenticated. On a replay by key, the stored body is returned with the header `Idempotent-Replayed: true` [P].

**Error responses**

A single format:

```json
{ "error": { "code": "insufficient_funds", "message": "..." } }
```

| Status | `code` | When |
|---|---|---|
| 422 | `validation_error` | Body is not JSON, the schema or a rule from the table above is violated; details in `error.details` |
| 422 | `self_payment` | Payee equals payer |
| 422 | `firm_not_found` | Payer or payee not found |
| 422 | `insufficient_funds` | Balance does not cover the batch total |
| 422 | `balance_overflow` | The payee's new balance does not fit in `INTEGER` |
| 422 | `idempotency_key_reuse` | Same key with different request content |
| 503 | `busy` | Lock wait or pool connection wait timed out; header `Retry-After: 1` |
| 500 | `internal_error` | Unexpected error; details only in logs |
| 404 | `not_found` | Unknown path |
| 405 | `method_not_allowed` | Known path, wrong method |
| other 4xx | `http_error` | Any other HTTP-level error raised by the framework |

A body that is not valid UTF-8 is a `validation_error` too, not a 400. When the body is rejected and the request's `Content-Type` is not JSON (curl's `-d` sends `application/x-www-form-urlencoded`), the message says to send `Content-Type: application/json`. `details` carry only `loc`, `msg` and `type`, never the rejected input. A body over the balancer's 8 MB limit is refused by nginx with its own 413 page before it reaches the service.

Rationale [P]: the task requires 422 for a rejected request and does not distinguish reasons, so all rejections caused by the request return 422, and the reason is carried in `code`. FastAPI's default validation error handler is overridden so that the format is uniform. For a key reused with different content, 422 matches the recommendation of the IETF draft.

### 8.2. Processing algorithm

**Before the transaction** (no database access):

1. Parse and validate the request by the rules in section 8.1.
2. Convert amounts to cents and compute the batch total.
3. Sum the credits per payee.
4. Build the set of UUIDs: payer and all payees.
5. Compute `request_hash` if a key was provided.

**Transaction** (default isolation level, Read Committed):

1. Set `lock_timeout` for this transaction from configuration with `SELECT set_config('lock_timeout', :value, true)`. `SET LOCAL` does not accept a bound parameter.
2. If a key was provided: `INSERT INTO idempotency_keys ... ON CONFLICT (payer_firm_uuid, key) DO NOTHING RETURNING id`.
   - Row inserted: continue.
   - Row not inserted: read the existing one. Hash matches: return the stored body with 201. Hash differs: 422 `idempotency_key_reuse`.
3. Lock the firms with one query:
   ```sql
   SELECT id, uuid, balance_cents
   FROM firms
   WHERE uuid = ANY(:uuids)
   ORDER BY id
   FOR NO KEY UPDATE;
   ```
4. If not all UUIDs were found: 422 `firm_not_found`.
5. If the payer's balance is less than the batch total: 422 `insufficient_funds`.
6. If any payee's new balance exceeds the `INTEGER` bound: 422 `balance_overflow`.
7. Insert all payments in one operation and obtain their `id` values in request order.
8. Update balances: payer minus the batch total, each payee plus its sum.
9. If a key was provided: write the response body to `idempotency_keys.response_body`.
10. `COMMIT`, respond 201.

Any rejection in steps 2–6 rolls back the whole transaction, including the key row.

**Database error handling**

| Situation | Action |
|---|---|
| Lock wait timed out (SQLSTATE 55P03) | Roll back, 503 `busy` |
| No free connection in the pool | 503 `busy` |
| Deadlock (SQLSTATE 40P01) | Roll back, 503 `busy`, log at ERROR level: by construction this should not occur |
| Other | Roll back, 500 `internal_error` |

### 8.3. Rationale for key steps

**Why the key is inserted first.** The unique index makes a second request with the same key wait for the outcome of the first. If the first commits, the second gets the stored response; if it rolls back, the second executes itself. A separate "in progress" state and a 409 response are not needed. In addition, the acquisition order is the same for all transactions: the key first, then firms in ascending `id`. A probe confirmed both outcomes, and that `lock_timeout` bounds this wait (section 17). Tests I5 and C8 re-confirm it in the project.

**Why one query for lookup and locking.** It finds the firms, checks their existence and locks them in a single consistent order. Before it, firms are not read at all, so stale data cannot exist in the session. Additionally, the query runs with `populate_existing`: if a `Firm` object is somehow already in the session, its attributes are overwritten with fresh values. In SQLAlchemy 2.1.9 a `with_for_update` query refreshes such objects even without the option (probe, section 17); the option stays so that the behavior does not depend on the library version.

**Why `FOR NO KEY UPDATE` rather than `FOR UPDATE`.** The balance is not a key column, so the weaker mode is sufficient; a plain `UPDATE` of non-key columns takes the same mode. Inserting a payment with a foreign key to the same firm from another transaction does not wait in this case. In SQLAlchemy this is `with_for_update(key_share=True)`.

**Why the balance check is in code rather than in `WHERE`.** The row is locked, the value is current, the condition is in one place, and it distinguishes the reasons for rejection.

**Why the lock order is specifically by `id`.** `id` is immutable, so the order is the same for all transactions at any time. The documentation does not state outright that `ORDER BY ... FOR NO KEY UPDATE` acquires locks in sorted order. A probe supports it (section 17): the query plan puts the `LockRows` node above `Sort`, so rows are locked as they leave the sort, and the same workload without `ORDER BY` deadlocks. In the project the plan has the same shape on PostgreSQL 18.6, and without `ORDER BY` the cycle test C3 deadlocks while the two-firm crossing test C2 does not (section 13.7); why C2 stays clean is not established.

## 9. Concurrency and invariants

### 9.1. Invariants

| No. | Invariant | Ensured by |
|---|---|---|
| I-1 | The service never drives a payer's balance below zero | Check under the payer's row lock |
| I-2 | The sum of all balances does not change | Debit and credits in one transaction; amounts equal by construction |
| I-3 | The debit from the payer equals the sum of this batch's `payments` rows | One transaction |
| I-4 | A batch is applied entirely or not at all | One transaction, rollback on any rejection |
| I-5 | Each key is executed at most once | Unique index and key insert in the same transaction |
| I-6 | Instances do not share in-memory state | All coordination in the database |

### 9.2. Scenario analysis

- **Two batches from the same payer at the same time.** The second waits on the payer's row and, after the first commits, sees the already reduced balance.
- **Crossing payments A→B and B→A.** Both transactions request locks in `id` order, so one gets both rows and the other waits. There is no wait cycle.
- **Many payers, one payee.** Transactions queue on the payee's row. The final balance equals the sum of all credits.
- **Two requests with the same key at the same time.** The second waits on the unique index, then receives the stored response.
- **Instance failure mid-transaction.** The database rolls back the transaction and releases the locks.
- **Lost response after commit.** The client retries with the same key and receives the stored response. Without a key, the retry creates a second batch; this is a known limitation (section 15).

### 9.3. Behavior under load

- Locks are per row: payments between different firms proceed in parallel.
- Batches from the same payer must run one after another under any correct design.
- A "hot" payee is a consequence of the schema, where the balance is stored in the row; the chosen approach holds the row slightly longer than a conditional `UPDATE`.
- Hold time equals the transaction duration, so the transaction contains nothing except four to five database queries: parsing and checks are moved outside, and payments are inserted in one operation.
- Batch size is bounded so that one request does not hold locks on thousands of firms for long. The bound and its measurement are in section 5.7.
- Each lock wait is bounded by `lock_timeout`: when it is exhausted, the client receives 503, not an indefinite wait. The bound applies to each wait, not to the request: a request that waits on several rows in turn can wait up to `lock_timeout` for each of them. nginx gives up on a response after 60 s (`proxy_read_timeout`) and returns its own 504 page, while the transaction may still commit.
- The real limit is connections: a waiting transaction occupies a connection. Pool size multiplied by the number of instances must not exceed the database's connection limit. On the stand this is 3 × 10 = 30 of PostgreSQL's default 100.

## 10. Configuration

Via environment variables (pydantic-settings).

| Variable | Default | Purpose |
|---|---|---|
| `DATABASE_URL` | — | Connection string |
| `MAX_PAYMENTS_PER_REQUEST` | 1000 | Batch size limit |
| `MAX_DESCRIPTION_LENGTH` | 500 | Description length limit |
| `LOCK_TIMEOUT_MS` | 5000 | Lock wait limit |
| `DB_POOL_SIZE` | 10 | Connection pool size |
| `DB_POOL_TIMEOUT_S` | 5 | Wait limit for a connection from the pool |

The Compose file also reads `DB_PORT` (default 5432) and `LB_PORT` (default 8000), the host ports of the database and the balancer; host-side tests follow `DB_PORT`.

All default values are **[P]**, except `MAX_PAYMENTS_PER_REQUEST`, which is **[R]** (section 5.7). The pool is created without overflow (`max_overflow=0`) and with a connection health check before handing out a connection. Synchronous endpoints run in AnyIO's thread pool, which allows 40 threads by default (probe, section 17). At startup the application sets that limit to `DB_POOL_SIZE`, so a request waits for a thread instead of failing on pool checkout. The limit can be changed only from the event loop (the lifespan handler), not from a worker thread.

## 11. Repository structure

```
app/
  main.py          application creation, error handlers
  api.py           endpoint: HTTP <-> service
  schemas.py       request and response models, amount parsing
  service.py       bulk payment transaction
  idempotency.py   key handling
  models.py        ORM models
  db.py            engine, session factory
  config.py        settings
  errors.py        domain errors and their codes
migrations/        Alembic
tests/
  unit/            amount parsing, request hash; no database
  db/              schema and migrations
  api/             contract
  concurrency/     races, locks, idempotency under parallel load
scripts/
  seed.sql         the three firms from the task
  init-test-db.sql creates the test database in the db container
  load.py          load scenarios
docs/
  spec.md          this document
  plan.md          implementation plan
  agent-workflow.md         how the AI agents were used
  verification-notes.md     measured and observed results
ai-logs/           logs of AI work (section 14.3)
deploy/
  nginx.conf       balancer configuration
.github/workflows/ci.yml    make check; the stand on Docker Engine
Dockerfile         image for migrate and app
docker-compose.yml db, migrate, app (three replicas), lb
Makefile           up, seed, reset (Compose); test, lint, check, load (uv on the host)
.python-version    3.14
sample-request.json          the request from page 4 of the task
format-example-request.json  the request from page 3 of the task
README.md
```

Boundary of responsibility: `api.py` does not know about SQL, `service.py` does not know about HTTP. Domain errors are translated into responses in one place.

## 12. Observability

- A structured log entry per request: request identifier, payer, number of payments, total, outcome (`created`, `replayed`, rejection code), duration. The request identifier comes from the `X-Request-Id` header set by the balancer, or is generated if the header is absent. The entry also names the instance (the container host name), so a request can be followed across replicas.
- `GET /healthz`: database connection check.
- Payment descriptions are not written to logs.

## 13. Verification

The verification question: is the service built according to this specification?

### 13.1. Principles

- Tests run against a real PostgreSQL in Docker. SQLite is not used, because lock behavior is what is being verified.
- Each test starts from a known table state.
- Concurrency tests do not rely on chance: threads start simultaneously through a barrier, and the scenario is repeated many times.
- A separate deterministic test proves that the lock is actually taken (C4).
- After each concurrency test, invariants I-1 and I-2 are checked.
- The concurrency tests are themselves checked by mutation (section 13.7): a test that stays green when the lock is removed proves nothing.

### 13.2. Unit tests

| No. | Check |
|---|---|
| U1 | Valid amounts: `"1200.75"`→120075, `"300"`→30000, `"5800.5"`→580050, `"0.01"`→1, `"007"`→700 |
| U2 | Invalid amounts: `"0"`, `"0.00"`, `"-1"`, `"+1"`, `"1.234"`, `"1."`, `".5"`, `"1e3"`, `" 1"`, `"1 "`, `"1\n"`, `"1,5"`, `""`, `"NaN"`, digits from other scripts, a string of 5,000 digits, a number instead of a string, `null` |
| U3 | An amount above 2,147,483,647 cents is rejected |
| U4 | Request hash: identical for `"300"` and `"300.00"`; differs for a different order, amount, payee or description |
| U5 | Amount formatting in responses: always two digits after the decimal point |

### 13.3. Contract tests

| No. | Scenario | Expectation |
|---|---|---|
| A1 | Example from the task | 201; balances 3674875 / 170075 / 1405050; three rows in `payments` |
| A2 | Total exceeds balance | 422 `insufficient_funds`; balances and `payments` unchanged |
| A3 | Total equals balance | 201; payer balance 0 |
| A4 | Total exceeds balance by 1 cent | 422 `insufficient_funds` |
| A5 | Unknown payee among known ones | 422 `firm_not_found`; nothing changed |
| A6 | Unknown payer | 422 `firm_not_found` |
| A7 | Payment to oneself | 422 `self_payment` |
| A8 | One payee in several payments | 201; credit equals the sum; `payments` rows are separate |
| A9 | Empty list, limit exceeded, extra field, long description, description with U+0000 | 422 `validation_error` |
| A10 | UUID in uppercase | 201 |
| A11 | Payee balance overflow | 422 `balance_overflow`; nothing changed |
| A12 | Error format | All 4xx and 5xx responses use the single format |
| A13 | Payment order in response | Matches request order |
| A14 | Body that is not a JSON object: invalid JSON, `[]`, `"x"`, `null`, `123`, an empty body, JSON sent as `text/plain` | 422 `validation_error` |
| A15 | Body of a successful response | Contains no balance |
| A16 | The request from page 3 of the task on the seeded database | 201; balances 4849925 / 170075 / 230000; two rows in `payments` |

### 13.4. Idempotency

| No. | Scenario | Expectation |
|---|---|---|
| I1 | Same key, same body | Second response 201 with the same body and `Idempotent-Replayed: true`; balances changed once |
| I2 | Same key, different body | 422 `idempotency_key_reuse`; nothing changed |
| I3 | Rejection for balance, top-up, retry with the same key | First 422, second 201 |
| I4 | One key used by two different payers | Requests are independent |
| I5 | N simultaneous requests with one key | All 201 with the same body; one execution |
| I6 | Two identical requests without a key | Two executions (documented behavior) |
| I7 | State of the key table | All rows have `response_body` filled; no rows after a rejection |
| I8 | `"300"` and `"300.00"` with one key | Replay, not a conflict |
| I9 | Key that is empty, 256 characters long, or contains a space or a non-ASCII character | 422 `validation_error` |

### 13.5. Concurrency and load

| No. | Scenario | Expectation |
|---|---|---|
| C1 | N parallel batches from one payer, balance covers K | Exactly K responses 201, the rest 422; final balance equals the start minus K totals and is not negative |
| C2 | Crossing payments A↔B, repeated many times | All requests complete, no deadlocks, I-2 holds |
| C3 | Cycle A→B→C→A and a random mix of payments with a fixed seed | Same |
| C4 | A firm's row is locked by an external connection | Request waits; after release it completes and reflects the changed balance |
| C5 | Lock held longer than `LOCK_TIMEOUT_MS` | 503 `busy` with `Retry-After`; nothing changed |
| C6 | Many payers, one payee | Payee balance equals the start plus the sum of all credits |
| C7 | C1, C2 and I5 over HTTP against the service running as several processes | Results as in C1, C2 and I5 |
| C8 | Another transaction holds an uncommitted row for the same key longer than `LOCK_TIMEOUT_MS` | 503 `busy`; nothing changed |
| C9 | No free connection in the pool within `DB_POOL_TIMEOUT_S` | 503 `busy` |
| L1 | `scripts/load.py` through the balancer: uniform load, "hot" payer, "hot" payee, maximal batch (1,000 payments to 1,000 distinct payees) | Report: requests per second, p50/p95/p99; for the maximal batch, its duration against the 250 ms budget of section 5.7. After the run, I-1 and I-2 hold and there are no 500 responses |

Scenario C7 models multiple instances: Uvicorn processes do not share memory and communicate only through the database. It includes crossing payments because the lock order must hold across processes, not only within one. C7 runs inside the test suite, with processes; the real replicas behind the balancer are exercised by L1 and by checks S1–S3 (section 13.8). There are no pass/fail thresholds for L1: the goal is to replace the calculated estimate from assumption 7 with a measurement and record the result in the README.

### 13.6. Static checks

- `ruff check`, `ruff format --check`, `mypy` with no errors.
- Migrations apply to an empty database and roll back.
- All checks run with one command and in CI.

### 13.7. Mutation check of the concurrency tests

Each mutation is applied to the service code alone, the concurrency tests are run, and the code is restored.

| No. | Mutation | Expectation |
|---|---|---|
| M1 | Remove the locking clause from the lock query | C1 and C4 fail |
| M2 | Remove `ORDER BY` from the lock query | C2 or C3 fails with deadlocks |
| M3 | Lock the payer in one query and the payees in a second | C2 fails with deadlocks |

A mutation that leaves every test green means the tests are too weak and must be strengthened. The observed results are recorded in `docs/verification-notes.md`: M1 failed C1, C2, C3, C4 and C6; M2 failed C3 in 3 of 3 runs while C2 stayed green; M3 failed C2 and C3. No test needed strengthening.

### 13.8. Checks of the run stand

| No. | Check | Expectation |
|---|---|---|
| S1 | `docker compose up -d --wait`, then `docker compose ps -a` | `db` and `lb` running, three `app` containers healthy, `migrate` exited with code 0 |
| S2 | 30 requests to `GET /healthz` through the balancer | The `X-Upstream` header takes exactly three distinct values |
| S3 | Stop one `app` container; send the sample request five times with one idempotency key; start the container again | Five responses 201, four of them replays; balances changed once |

Docker was not available where the design probes ran, so S1–S3 were the first real verification of the stand.

The development machine runs Podman 5.8 (rootful, netavark) behind the `docker` command, with Docker Compose v5.1 as the compose provider. Reviewers will most likely run Docker Engine or Docker Desktop. S1–S3 are therefore run locally on Podman, and CI on GitHub-hosted runners exercises the same `docker compose` commands on Docker Engine. The README states where each was verified. If the stand behaves differently under Podman (replica count, service-name DNS returning every replica, `--wait`), the difference is reported with evidence, not worked around in the Compose file.

Outcome: S1–S3 passed on Podman 5.8.2 with no workaround; the only visible difference is that Podman's `ps` does not print `(healthy)`. Under Podman a stopped replica's address times out instead of refusing the connection, so the first request after S3's stop took 2 s (`proxy_connect_timeout`) before nginx passed it on. The CI job `stand` brings the stand up on Docker Engine with `make up`, posts the sample request (201, Pinecrest 3674875) and checks three distinct `X-Upstream` values over 30 health checks; it passes.

## 14. Validation

The validation question: does the service solve the problem stated in the task?

### 14.1. Requirements traceability

| Requirement | Section | Check |
|---|---|---|
| R1 | 8.1 | A1, A9, A14 |
| R2 | 8.2, step 5 | A2, A4, C1 |
| R3 | 8.2, steps 7–10 | A1, A3, A8 |
| R4 | 5.4, 5.6, 9 | C1–C9, mutation check M1–M3, stand checks S1–S3 |
| R5 | 6, 7 | All tests run against PostgreSQL |
| R6 | 8.1 | U1–U3 |
| R7 | 7.1 | Migrations; A1 uses the `INSERT` from the task unchanged |
| R8 | — | A1 and a manual run per the README |
| R9 | 6 | Dependency review |
| R10 | 11 | A1, A16, acceptance run (14.2) |
| R11 | — | Submission checklist (14.3) |

### 14.2. Acceptance run

Performed on a clean clone, strictly following the README, with no knowledge beyond it:

1. Clone the repository.
2. Start with one command. The stand comes up as three replicas behind the balancer (check S1).
3. Load the three firms from the task.
4. Send the example from the task with `curl` to the balancer on port 8000.
5. Verify: response 201, balances $36,748.75 / $1,700.75 / $14,050.50.
6. Send the same request three more times: two responses 201, then 422 (the balance covers three batches of four: 50,000 − 3 × 13,251.25 = 10,246.25).
7. Reset the database to the three seeded firms with one command, send the request from page 3 of the task, and verify: response 201, balances $48,499.25 / $1,700.75 / $2,300.00.
8. Run the tests with one command.
9. Run stand checks S2 and S3.

Step 6 checks understanding of the task on the task's own data: the balance after the third batch is less than the batch total. Step 7 covers the second request the task shows; the task asks that "the sample requests" can be sent.

### 14.3. Submission checklist

- The commit history reflects the progress of the work.
- `ai-logs/` contains the logs of the working sessions: the design sessions, the validation and execution session (with each subagent's transcript) and the review session. The only change to them is masking the account email and the home directory path. The README names the tools, says where each fit into the workflow, and states plainly what was masked and which sessions were left out (aborted early attempts).
- The README contains: the solution and approach, encountered problems, assumptions, possible improvements, how to run and verify, answers to the two questions.
- The assumptions section of the README matches section 4; the improvements section matches section 15.

### 14.4. What automated checks do not cover

- Fit with the reviewers' infrastructure and team practices: nothing is known about them.
- The assumptions in section 4: they are reasonable but not confirmed by the client.
- Behavior when the database itself fails, and under replication.
- Requests the reviewers may send beyond the task's two examples. The task defines no limits, no rule for unknown fields and no endpoint path. A request that exceeds our limits, carries an extra field or omits `description` gets 422 here. These choices are assumptions 8–11.
- The stand runs on one host: three replicas, one nginx and one PostgreSQL in Compose. It shows the load-balanced arrangement the task describes, not a highly available deployment: the balancer and the database are single points of failure.

## 15. Known limitations and improvements

1. A request without a key is not protected against retries. Improvement: make the key mandatory.
2. Keys are never deleted. Improvement: a time-to-live and periodic cleanup.
3. `INTEGER` limits balances to about $21.4 million. Improvement: `BIGINT`.
4. A "hot" payee queues payers. Improvement: a journal of credits with deferred balance recalculation.
5. No authentication and no authorization check for the debit.
6. No `CHECK (balance_cents >= 0)` as a last line of defense (assumption 4).
7. Payments have no creation time and no batch identifier.
8. No metrics or tracing.
9. The number of connections grows with the number of instances. Improvement: a database-side pool (PgBouncer).
10. If `Idempotency-Key` is sent more than once, the first value is used. Improvement: reject such a request.
11. The stand is not highly available: one balancer, one database. nginx resolves replica addresses only at start, so the balancer is restarted after replicas are recreated.
12. A client with more than 1,000 payments must split them into several requests; atomicity then holds per request, not across them.
13. `lock_timeout` bounds each lock wait, not the whole request (section 9.3). Improvement: a transaction-scoped `statement_timeout` or a request deadline below the balancer's read timeout.
14. A body over 8 MB gets nginx's own 413 page instead of the single error format. Improvement: an nginx `error_page` that returns the JSON format.

## 16. Left to the implementer

- Exact dependency versions.
- Choices this document leaves open (how balances are written, how payment ids are read back) are fixed in `docs/plan.md`.
- How the database is cleaned between tests.
- Log format.
- The name and exact fields of `error.details` for validation errors.

## 17. Sources

Checked against primary sources during design (21–24.11.2026):

- PostgreSQL, Index Uniqueness Checks: waiting when inserting a conflicting key: https://www.postgresql.org/docs/current/index-unique-checks.html
- PostgreSQL 18, Explicit Locking: row-level lock modes, deadlocks: https://postgresql.org/docs/18/explicit-locking.html
- PostgreSQL 14, Transaction Isolation: Read Committed behavior and serialization errors: https://www.postgresql.org/docs/14/transaction-iso.html
- PostgreSQL feature matrix: purpose of `FOR NO KEY UPDATE`: https://www.postgresql.org/about/featurematrix/detail/select-for-no-key-updateselect-for-key-share-lock-modes/
- IETF, The Idempotency-Key HTTP Header Field, revision 07 (expired 18.04.2026, not a standard): https://datatracker.ietf.org/doc/draft-ietf-httpapi-idempotency-key-header/
- Stripe, Idempotent requests: the result-storage model: https://docs.stripe.com/api/idempotent_requests
- SQLAlchemy 2.0, `populate_existing` option (source text of the documentation): https://sources.debian.org/src/sqlalchemy/2.0.44%2Bds1-1/doc/build/orm/queryguide/api.rst
- Confluent, Kafka Message Delivery Guarantees (vendor documentation): https://docs.confluent.io/kafka/design/delivery-semantics.html
- nginx, ngx_http_upstream_module: a domain name that resolves to several addresses defines several servers: https://nginx.org/en/docs/http/ngx_http_upstream_module.html
- nginx, ngx_http_proxy_module, `proxy_next_upstream`: a non-idempotent request is not passed to the next server once it has been sent: https://nginx.org/en/docs/http/ngx_http_proxy_module.html
- Docker, Compose Deploy Specification (`deploy.replicas`): https://docs.docker.com/reference/compose-file/deploy/
- PostgreSQL, Connections and Authentication: `max_connections` is typically 100 by default: https://www.postgresql.org/docs/current/runtime-config-connection.html
- PostgreSQL 19 release announcement of 2026-11-19: https://www.postgresql.org/about/news/postgresql-19-released-3449/

Confirmed by throwaway probes on 2026-11-21 and 2026-11-24 (PostgreSQL 16.15, psycopg 3.3.9, SQLAlchemy 2.1.9, FastAPI 0.165.0, Pydantic 2.13.5, AnyIO 4.15.1). The probes are not part of the repository; the project's tests re-confirm each point on the pinned versions.

- `SET LOCAL lock_timeout` with a bound parameter fails with a syntax error. `set_config('lock_timeout', ..., true)` works and resets at commit.
- `INSERT ... ON CONFLICT DO NOTHING` waits for an uncommitted conflicting row. If that transaction commits, the insert returns no row and the next statement sees the committed row. If it rolls back, the insert succeeds.
- `lock_timeout` bounds both that wait and a row-lock wait. The error is SQLSTATE 55P03; SQLAlchemy raises it as `OperationalError` with `orig.sqlstate` set.
- The plan of the lock query is `LockRows` above `Sort`. 2,400 crossing transactions with `ORDER BY id` gave 0 deadlocks; the same run without `ORDER BY` gave 80. The project did not reproduce the 80 with two firms on PostgreSQL 18.6; it reproduced deadlocks without `ORDER BY` with three firms (section 13.7).
- Locking the payer and the payee in separate queries deadlocks under a forced interleaving (SQLSTATE 40P01).
- After waiting for a row lock, the lock query returns the value committed by the other transaction.
- A firm row held `FOR NO KEY UPDATE` does not block an insert into `payments` that references it. Held `FOR UPDATE`, it does.
- PostgreSQL rejects U+0000 in `TEXT`. Through SQLAlchemy this is a `DataError`, which would surface as a 500 without the validation rule.
- `JSONB` reorders object keys; `JSON` returns the stored text unchanged.
- Pool exhaustion raises `sqlalchemy.exc.TimeoutError`, which is not a `DBAPIError`.
- `add_all` followed by `flush` assigns ids in list order (checked for 1,000 rows).
- FastAPI answers 422 to invalid JSON, to a body that is not an object, to an empty body and to JSON sent as `text/plain`.
- AnyIO's default thread limit is 40. It can be changed in the lifespan handler but not from a worker thread.
- One transaction of 1,000 payments to 1,000 distinct payees held its locks for 109 ms (median of 7 runs; table in section 5.7).

Docker was not available where the probes ran, so the design of the run stand (section 5.6) rested on the nginx and Compose documentation listed above. Checks S1–S3 have since run on Podman and the CI job on Docker Engine (section 13.8).

Claims the project's own tests must re-confirm on the pinned versions:

- the lock acquisition order for `ORDER BY ... FOR NO KEY UPDATE` (C2, C3, mutation check);
- the behavior of `ON CONFLICT DO NOTHING` after waiting on another transaction (I5);
- the effect of `lock_timeout` on a unique-index wait, and error code 55P03 (C5, C8);
- a 422 response to invalid JSON and to non-object bodies in the FastAPI version in use (A9, A14);
- the thread limit for synchronous endpoints (section 10).
