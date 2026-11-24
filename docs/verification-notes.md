# Verification notes

Observed results of the checks that spec section 13 asks to record. Everything below was run
on 2026-11-24 on an Apple Silicon Mac (macOS, `darwin`), with the tests on the host
(`uv run pytest`, Python 3.14.10) against the stand's `db` container on `127.0.0.1:5432`.

| Component | Version in use | Version in the design probes (spec section 17) |
|---|---|---|
| PostgreSQL | 18.6 (`postgres:18` image) | 16.15 |
| psycopg | 3.3.9 | 3.3.9 |
| SQLAlchemy | 2.1.9 | 2.1.9 |
| FastAPI / Starlette | 0.165.0 / 1.10.0 | 0.165.0 |
| AnyIO | 4.16.0 | 4.15.1 |

The concurrency tests are in `tests/concurrency/test_races.py` (C1, C2, C3, C6, I5) and
`tests/concurrency/test_locks.py` (C4, C5, C8, C9, the key-wait outcomes, the thread limit
and the statement order of spec 8.2). Without mutations the whole suite passed five runs in a
row (140 tests, 4.1 to 4.3 s per run; the concurrency tests take about 3.7 s of that).

## Mutation check

Procedure for each mutation (spec section 13.7): `git status` clean under `app/`, the change
applied to `app/service.py` alone, `uv run pytest tests/concurrency -v`, then
`git checkout app/service.py`. No mutation was committed.

`test_statement_order[with_key]` and `test_statement_order[without_key]` fail under all three
mutations, because they pin the exact text of the lock query. The behavioral tests are listed
separately: they fail on their own, without the statement-order test.

### M1: no locking clause

Change: `.with_for_update(key_share=True)` removed from the lock query.

Result: 7 failed, 8 passed, 2.2 s.

| Test | How it failed |
|---|---|
| `test_c1_one_payer_many_batches` | first round: `{'created': 6, 'insufficient_funds': 4}` instead of 3 and 7 |
| `test_c2_crossing_payments` | lost update: A and B ended at `(10000100, 9999900)` instead of `(10000000, 10000000)` |
| `test_c3_cycle_and_random_mix` | `I-2 violated: the sum of balances moved`, `5677758 == 5550000` |
| `test_c4_request_waits_for_row_lock` | `('created', {...}) == ('insufficient_funds', None)`: the request checked the stale 500.00 instead of the 300.00 committed while it waited |
| `test_c6_hot_payee` | lost updates on the payee: `201000 == (200000 + (20 * 1000))` |
| `test_statement_order[*]` | the lock query became `other: SELECT firms.id, ... FROM firms WHERE ...` |

C5 and C8 still pass under M1, as they should, for different reasons:

- C5: without the locking clause the request no longer waits at the lock query. But the
  foreign-key check of the payments insert takes `FOR KEY SHARE` on the payer row, which
  conflicts with the external `FOR UPDATE`. So the request still waits there and runs into
  `lock_timeout`.
- C8: the request waits on the `idempotency_keys` insert and never reaches the firm query,
  so M1 cannot affect it.

### M2: no ORDER BY

Change: `.order_by(Firm.id)` removed from the lock query.

Result: 3 failed, 12 passed, 24.9 s. Two more runs of `tests/concurrency/test_races.py` under
the same mutation took 17.4 s and 12.4 s. Each deadlock costs PostgreSQL's 1 s `deadlock_timeout`.

| Test | How it failed |
|---|---|
| `test_c3_cycle_and_random_mix` | `busy` outcomes: `Extra items in the left set: 'busy'`. The service logged 40 `deadlock detected` records at ERROR level; the two further runs logged 41 and 16. It failed in 3 of 3 runs. |
| `test_statement_order[*]` | the lock query lost `ORDER BY firms.id` and was labelled `other: ...` |

Excerpt from the captured log of `test_c3_cycle_and_random_mix`:

```
ERROR    app.service:service.py:146 deadlock detected in a bulk payment transaction: deadlock detected
DETAIL:  Process 5908 waits for ShareLock on transaction 24525; blocked by process 5897.
Process 5897 waits for ExclusiveLock on tuple (1,7) of relation 56818 of database 16385; blocked by process 5870.
Process 5870 waits for ShareLock on transaction 24527; blocked by process 5908.
CONTEXT:  while locking tuple (1,2) in relation "firms"
```

`test_c2_crossing_payments` passed under M2 in all three runs: 3,000 crossing transactions
with no deadlock and no `busy`. The design probe saw 80 deadlocks in 2,400 crossing transactions
(spec section 17); that probe is not in the repository, so the difference is not explained. C2
therefore says nothing about M2. It is the test that catches M3, below.

Further observations, captured after the runs above. Each was taken by running one test under the
same mutation with `auto_explain` (`log_min_duration = 0`) enabled on the test database. Both
settings were reset afterwards, and `app/service.py` was restored with `git checkout` and checked
clean.

- Plan of the M2 lock query (`... WHERE firms.uuid IN (...) FOR NO KEY UPDATE`): `LockRows` over
  `Bitmap Heap Scan on firms` over `Bitmap Index Scan on uq_firms_uuid`.
  - C2: in all 1,000 lock queries.
  - C3: in all 171 captured. The container log dropped about 6% of the statements in this run,
    so 171 is not the number of lock queries C3 ran.
- Row versions of `firms`, from `pg_stat_user_tables`:
  - C2 under M2: 2,000 updates, of which 1,969 were HOT and 31 went to a new page.
  - The following C3 run under M2: 345 updates, 328 HOT and 17 to a new page.
- In C3, the deadlock reports name tuples on page 1 of `firms`, for example `(1,7)` and `(1,2)`.
  Inferred from these tuple ids, not observed directly: some rows had moved off page 0.
- The ORM sends a transaction's balance updates as one executemany in primary-key order. A
  request from Nair (id 3) to Pinecrest (id 1) and Lopez (id 2) updates ids 1, 2, 3 in that order.

A transaction takes all its row locks in the `SELECT ... FOR NO KEY UPDATE`, before any of its
own updates run. Its update order therefore cannot set its own lock order; at most, it shapes the
heap and index layout that later transactions scan.

Why C2 stays deadlock-free without `ORDER BY` while C3 does not is not established. Candidates,
none verified:

- (a) A bitmap heap scan visits rows in the TID order of their index entries. A HOT update keeps
  the index entry on the chain's root line pointer, which is the original insertion position:
  A before B, the same as id order here. Only a non-HOT update gives a row an index entry at a
  new TID.
- (b) In C2, every transaction updates both rows, in the same order (A, then B), so both rows
  get new versions in the same transactions. In C3, transactions update different subsets of
  firms, so the TID order can drift from id order and differ between transactions that start at
  different times.
- (c) A difference from the probe's setup (PostgreSQL 16.15 there, 18.6 here; the probe's
  statements and data are unknown) accounts for the probe's deadlocks.

Spec 13.7 expects "C2 or C3" to fail under M2; here C3 is the test that does.

### M3: payer and payees locked in two queries

Change: the single lock query replaced by two. The first is
`select(Firm).where(Firm.uuid == payer).with_for_update(key_share=True)`. The second is
`select(Firm).where(Firm.uuid.in_(payees)).order_by(Firm.id).with_for_update(key_share=True)`.
Both keep `populate_existing=True`, and the two results are merged.

Result: 4 failed, 11 passed, 66.5 s.

| Test | How it failed |
|---|---|
| `test_c2_crossing_payments` | first round: `{'busy': 47, 'created': 3}` instead of `{'created': 50}`; 44 `deadlock detected` records at ERROR level. The remaining busy outcomes timed out waiting behind the deadlocked transactions. |
| `test_c3_cycle_and_random_mix` | `busy` outcomes; 149 `deadlock detected` records |
| `test_statement_order[*]` | two firm queries instead of one; the payer query lacks `ORDER BY firms.id` |

Excerpt from the captured log of `test_c2_crossing_payments`:

```
ERROR    app.service:service.py:146 deadlock detected in a bulk payment transaction: deadlock detected
DETAIL:  Process 6013 waits for ShareLock on transaction 24970; blocked by process 6008.
Process 6008 waits for ExclusiveLock on tuple (0,4) of relation 56818 of database 16385; blocked by process 6012.
Process 6012 waits for ShareLock on transaction 24969; blocked by process 6011.
Process 6011 waits for ExclusiveLock on tuple (0,5) of relation 56818 of database 16385; blocked by process 6013.
CONTEXT:  while locking tuple (0,5) in relation "firms"
```

Every mutation turned at least one behavioral test red, so the tests were not strengthened.

## Claims re-confirmed

These are the five claims at the end of spec section 17.

| Claim | Settled by | Result |
|---|---|---|
| Lock acquisition order for `ORDER BY ... FOR NO KEY UPDATE` | `test_c3_cycle_and_random_mix` (200 batches, cycle A→B→C→A, seed 20261121, 16 threads) with mutations M2 and M3; `test_c2_crossing_payments` (20 rounds × 50 crossing transactions) with M3; plans captured with `auto_explain` | Confirmed. With `ORDER BY id`, C3 had no `busy` outcome and logged no deadlock in any run. Without it (M2), C3 deadlocked in 3 of 3 runs. Locking the payer and the payees in separate queries (M3) deadlocks both C2 and C3. C2 shows that crossing payments do not deadlock with the code as written. It is not evidence for the `ORDER BY` half of the claim: it also stayed green under M2 (see M2). Plan as executed on PostgreSQL 18.6: every lock query captured with `auto_explain` during C2 and C3 ran as `LockRows` above `Sort (Sort Key: id)` above `Bitmap Heap Scan on firms` (`Bitmap Index Scan on uq_firms_uuid`). The container log was incomplete: it held 1,125 of the 1,200 expected lock queries, and other statements were missing in a similar proportion. This is the shape spec 8.3 relies on, with `LockRows` above `Sort`. A manual `EXPLAIN` on the three seed rows gives the same shape with a `Seq Scan` under the `Sort`. |
| `ON CONFLICT DO NOTHING` after waiting on another transaction | `test_i5_same_key_in_parallel` (10 rounds × 8 threads, one key), `test_key_wait_then_commit_replays`, `test_key_wait_then_rollback_executes` | Confirmed. Each I5 round gave exactly 1 `created` and 7 `replayed`, identical bodies, 3 payment rows, Pinecrest 3 674 875 and one key row. With another transaction holding the key row, the request waits. After that transaction commits, the request returns the committed response and changes nothing. After it rolls back, the request executes and claims the key. |
| `lock_timeout` bounds the unique-index wait; error code 55P03 | `test_key_wait_respects_lock_timeout` (C8), `test_c5_lock_timeout`, `test_c5_http_503` | Confirmed. With an uncommitted key row held elsewhere and `lock_timeout_ms=200`, the request returns `busy` after at least 200 ms, and the cause is SQLSTATE `55P03`. The row-lock wait (C5) behaves the same way. Over HTTP the response is 503 `busy` with `Retry-After: 1`. Nothing changed in any of these cases. |
| 422 for invalid JSON and non-object bodies in FastAPI 0.165.0 | `tests/api/test_validation.py::test_non_object_bodies` (`[]`, `"x"`, `null`, `123`, invalid JSON, empty body, JSON sent as `text/plain`, invalid UTF-8) and `::test_a9_schema_violations` | Confirmed. Every case gives 422 `validation_error`. |
| Thread limit for synchronous endpoints (spec section 10) | `test_thread_limit_matches_pool_size` | Confirmed on AnyIO 4.16.0. A fresh event loop reports 40 tokens. In the application's loop after the lifespan has run, the limit is 10, which is `DB_POOL_SIZE`. |

Also observed: with the only pooled connection checked out, `execute_bulk_payment` raises
`ServiceBusy` whose cause is `sqlalchemy.exc.TimeoutError` (`test_pool_exhaustion_is_busy`,
C9). This matches the spec 17 note that pool exhaustion is not a `DBAPIError`.

## Multi-process concurrency (C7)

`tests/concurrency/test_multiprocess.py` starts `uvicorn app.main:app --workers 4` on a free
port against the test database (fixture `live_server`). The fixture treats the server as ready
once `/healthz` answers 200 and all four workers have logged `Application startup complete.`.
Each thread posts with its own `httpx2.Client`. The processes share no memory, so they
coordinate only through the database.

| Test | Result |
|---|---|
| `test_c7_one_payer_over_http` | 10 parallel batches of 300.00 from a firm holding 1,000.00 gave three 201 and seven 422 `insufficient_funds`. 100.00 is left and the ledger is consistent. |
| `test_c7_same_key_over_http` | 8 parallel posts of the sample request with one key all returned 201. Exactly one lacked `Idempotent-Replayed`, and the eight bodies were byte-identical. 3 payment rows; Pinecrest 3 674 875. |
| `test_c7_crossing_over_http` | 10 rounds of 50 (25 A→B and 25 B→A, `"1"` each): every response was 201 with no 503. A and B were unchanged after every round and the ledger stayed consistent. |

The module passed five runs in a row, 1.8 to 1.9 s each. The whole suite now has 145 tests.

### Connections per process

While C7 ran, `lsof` showed how many database connections each worker held: 11, 10, 9 and 11.
That is 41 connections against 4 × `DB_POOL_SIZE` = 40.

The cause: when a worker's first requests arrived concurrently, each built its own engine,
because `functools.lru_cache` does not make `get_engine` run only once. A single worker with no
warm-up request showed the extra connections staying open:

- After two bursts of 10 concurrent requests it held 19 connections.
- After 10 s idle it still held 19.

Commit 2639070 fixed this. The engine is now built under a lock, and the lifespan builds it
before the first request. After the fix:

- The same single worker held 10 connections.
- The C7 workers held 10, 6, 9 and 10.
- After the load runs below, each stand replica held exactly 10 (`pg_stat_activity` grouped by
  client address).

Connection budget during `make test` (`max_connections` 100):

| Holder | Connections |
|---|---|
| Stand, when running | at most 30 |
| Test process's own engine | 10 |
| `live_server` | at most 40 |
| In-process concurrency engine | at most 50 |

`live_server` and the in-process concurrency engine never hold connections at the same time:

- The concurrency engine is disposed at the end of each module and again before `live_server`
  starts.
- `live_server` is stopped at the end of its module.

The peak is therefore 80 while `live_server` runs and 90 during the in-process concurrency
modules.

## Load run

All runs were made on 2026-11-24.

**Machine.** The host is an Apple M4 Pro (12 cores, 48 GiB memory) running macOS 26.6.2. The
stand runs in the Podman 5.8.2 machine (applehv, rootful, Fedora 43 arm64), which has 6 CPUs and
3.7 GiB (3,814 MiB) of memory.

**Versions.** Docker Compose v5.1.3, PostgreSQL 18.6, nginx 1.30.8, Python 3.14.10, Uvicorn
0.54.0, httpx2 2.16.2.

**Setup.** The load script ran in a one-off `app` container inside the Compose network and
posted to `http://lb`. The client, the balancer, the three replicas and the database therefore
shared the VM's 6 CPUs.

- Before the runs: `make up` (the stand includes commit 2639070), then S1 and S2 below.
- After the runs: `make reset`.

At the time of these runs `make load` ran:

```
docker compose run --rm --build app python scripts/load.py \
    --base-url http://lb \
    --database-url postgresql+psycopg://postgres:postgres@db:5432/firm_payments $(ARGS)
```

The runs, in order:

```
make load ARGS="--scenario uniform --requests 2000 --concurrency 32"
make load ARGS="--scenario hot-payer --requests 2000 --concurrency 32"
make load ARGS="--scenario hot-payee --requests 2000 --concurrency 32"
make load ARGS="--scenario max-batch --requests 40 --concurrency 4"
make load ARGS="--scenario max-batch --requests 40 --concurrency 1"
```

The last run is supplementary: it measures the maximal batch without lock contention.

`make load` has since moved to the host (`uv run python scripts/load.py --base-url
http://localhost:8000 ...`). From the host every request also crosses the runtime's port
forwarding into the VM, so figures from the current target are not comparable with the ones
below.

**What each run does.** Before every run the script:

- deletes the firms with ids from 1000 and their payments;
- inserts 1,100 firms (ids 1000 to 2099) with 10,000,000.00 each.

Batches other than max-batch carry three payments of 0.01 to 10.00 each.

| Scenario | Requests / concurrency | Wall time, s | Requests/s | p50, ms | p95, ms | p99, ms | Max, ms | Statuses | Exit |
|---|---|---|---|---|---|---|---|---|---|
| uniform | 2000 / 32 | 1.930 | 1036.5 | 30.5 | 54.3 | 62.7 | 72.2 | 201: 2000 | 0 |
| hot-payer | 2000 / 32 | 3.231 | 619.0 | 37.0 | 123.0 | 159.9 | 326.2 | 201: 2000 | 0 |
| hot-payee | 2000 / 32 | 3.188 | 627.4 | 35.0 | 123.5 | 157.8 | 209.9 | 201: 2000 | 0 |
| max-batch | 40 / 4 | 2.048 | 19.5 | 193.2 | 239.4 | 257.6 | 257.6 | 201: 40 | 0 |
| max-batch (supplementary) | 40 / 1 | 2.204 | 18.1 | 50.2 | 69.6 | 69.9 | 69.9 | 201: 40 | 0 |

How the figures were taken:

- Durations are measured by the client, from sending the request to the end of the response,
  through nginx.
- Percentiles are nearest-rank.

**A change to `scripts/load.py` after the runs.** After the runs above, one timing line in
`send()` was moved: the end-of-request timestamp is now taken before the HTTP client is returned
to the pool, and was taken after it before. The old timed region therefore also contained one
`SimpleQueue.put` on an unbounded queue, which never blocks; `timeit` puts the call at about
10 ns on the host. It could lengthen a client duration by that much, which is far below the
0.1 ms the figures are given to, so it cannot change any recorded figure. The edit moved only
where the per-request clock is read; every request still does the same work, so wall time and
requests/s are unaffected. The statuses, `X-Upstream` counts, ledger checks and server
`duration_ms` values do not pass through that line. The runs were not repeated after the edit.

Results common to all five runs:

- No run had a 503, a 500 or a request without a response.
- `X-Upstream` split the requests evenly over the three replicas: 667/666/667, 666/666/668,
  667/667/666, 13/14/13 and 14/13/13.

After every run, every check of the script passed:

- No balance was negative.
- The sum of all balances stayed at 1,100,005,250,000 cents: the 1,100 load firms plus the seed.
- Every load firm's balance equalled its start balance minus its payments plus its credits.
- The payment rows equalled the 201 responses times the payments per request: 6,000 in each of
  the first three runs and 40,000 in each max-batch run.

I deliberately tampered with the database to check that the script reports a failure.
During a separate host-side `uniform` run, a `psql` session added 1 cent to one load firm's
balance. The script reported I-2 and the ledger check as FAIL and exited with 1.

**Assumption 7.** It estimated hundreds of batches per second through one firm. Measured on
this machine through the balancer, one payer handled 619 batches per second and one payee 627.

### Maximal batch against the 250 ms budget (spec 5.7)

| Run | Client median, ms | Client max, ms | Server median, ms | Server max, ms |
|---|---|---|---|---|
| 40 requests, concurrency 4 (planned) | 196.0 | 257.6 | 190.6 | 256.4 |
| 40 requests, concurrency 1 (supplementary) | 50.3 | 69.9 | 49.2 | 68.8 |

The client medians here are `statistics.median`, the mean of the 20th and 21st of the 40 sorted
durations. The p50 in the table above is nearest-rank, the 20th. So 193.2 ms there and 196.0 ms
here are the same run under the two definitions, as are 50.2 ms and 50.3 ms.

The server values come from `duration_ms` in the replicas' request log records, taken from
`docker compose logs app` and filtered by the run's payer. `duration_ms` runs from the request
entering the middleware to the response, lock waits included.

**Planned run.** All 40 requests come from one payer, so they run one after another on its row
lock. Each duration includes waiting behind up to three other maximal batches.

- The median, 196.0 ms, is within the budget.
- The maximum is over it: 257.6 ms at the client and 256.4 ms at the server, about 7 ms above
  250 ms.

**Supplementary run.** Without contention, a 1,000-payment request takes 50.3 ms median and
69.9 ms at most, end to end. Locks are held only inside the transaction, which is part of the
request. So these are upper bounds on how long a maximal batch holds its locks: about a quarter
of the 250 ms budget.

For comparison, the spec 5.7 probe measured 109 ms of lock hold and 126 ms for the whole
transaction at 1,000 payments.

**Decision.** The batch limit stays at 1,000. The overrun in the planned run is queueing behind
other batches of the same payer.

## Stand checks

These checks were run on Podman 5.8.2 with Docker Compose v5.1.3. They were first done in
Task 1 and repeated here.

**S1.** Run after `make up`. `docker compose up -d --wait` exited 0, and every container was
reported `Healthy`. `docker compose ps -a` showed:

```
NAME                                   IMAGE                                    COMMAND                  SERVICE   CREATED          STATUS                    PORTS
ta-firm-payments-challenge-app-1       docker.io/library/firm-payments:latest   "uvicorn app.main:ap…"   app       13 seconds ago   Up 11 seconds
ta-firm-payments-challenge-app-2       docker.io/library/firm-payments:latest   "uvicorn app.main:ap…"   app       13 seconds ago   Up 10 seconds
ta-firm-payments-challenge-app-3       docker.io/library/firm-payments:latest   "uvicorn app.main:ap…"   app       13 seconds ago   Up 11 seconds
ta-firm-payments-challenge-db-1        docker.io/library/postgres:18            "postgres"               db        2 hours ago      Up 2 hours                127.0.0.1:5432->5432/tcp
ta-firm-payments-challenge-lb-1        docker.io/library/nginx:stable           "nginx -g daemon off;"   lb        2 hours ago      Up 7 seconds              0.0.0.0:8000->80/tcp
ta-firm-payments-challenge-migrate-1   docker.io/library/firm-payments:latest   "alembic upgrade head"   migrate   13 seconds ago   Exited (0) 1 second ago
```

Under Podman, `ps` does not print `(healthy)`. `docker inspect -f '{{.State.Health.Status}}'`
gave `healthy` for all three app containers. `up -d --wait` starts the one-shot `migrate`
service again each time; `alembic upgrade head` then has nothing to do.

**S2.** 30 requests to `GET http://localhost:8000/healthz` gave these `X-Upstream` counts:

| Replica | Requests |
|---|---|
| 10.89.2.87:8000 | 8 |
| 10.89.2.88:8000 | 13 |
| 10.89.2.89:8000 | 9 |

That is three distinct values. The distribution right after a balancer restart is not strict
round-robin.

**S3.** The check started on a freshly seeded database (`make reset`).
`docker stop $(docker compose ps -q app | head -1)` stopped `app-1` (10.89.2.88). Then
`sample-request.json` was posted five times with `Idempotency-Key: s3`:

| Request | Status | `Idempotent-Replayed` | `X-Upstream` | Time, s |
|---|---|---|---|---|
| 1 | 201 | absent | `10.89.2.88:8000, 10.89.2.89:8000` | 2.030 |
| 2 | 201 | true | `10.89.2.87:8000` | 0.007 |
| 3 | 201 | true | `10.89.2.89:8000` | 0.006 |
| 4 | 201 | true | `10.89.2.87:8000` | 0.005 |
| 5 | 201 | true | `10.89.2.89:8000` | 0.005 |

Request 1 took 2 s because nginx first tried the stopped replica. Its address no longer
answered, so the connect timed out after `proxy_connect_timeout` (2 s) instead of being refused.
nginx then passed the request to the next replica. Nothing had been sent to the stopped replica,
so passing the POST on was safe.

Afterwards:

- The five bodies were byte-identical.
- The balances changed once: Pinecrest 3,674,875, Lopez 170,075 and Nair 1,405,050.
- The database held 3 payment rows and 1 key row.

**Restore.** `make up` started the replicas again and restarted `lb`. The image had been rebuilt
by `make load --build`, so all three replicas were recreated with new addresses. S2 was then
run again with 30 requests:

| Replica | Requests |
|---|---|
| 10.89.2.103:8000 | 8 |
| 10.89.2.104:8000 | 13 |
| 10.89.2.105:8000 | 9 |

Again three distinct values. `make reset` then returned the database to the three seeded firms.

**Podman.** Nothing in the stand needed a Podman workaround:

- three replicas started;
- the service name resolved to every replica, since there were three `X-Upstream` values;
- `--wait` waited for health.

The only visible difference is the missing `(healthy)` in `ps`.
