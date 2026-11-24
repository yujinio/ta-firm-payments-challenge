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
  - C3: in all 171 that completed. The others failed with a deadlock (21 in this run).
- Row versions of `firms`, from `pg_stat_user_tables`:
  - C2 under M2: 2,000 updates, of which 1,969 were HOT and 31 went to a new page.
  - The following C3 run under M2: 345 updates, 328 HOT and 17 to a new page.
- In C3, the deadlock reports name tuples on page 1 of `firms`, for example `(1,7)` and `(1,2)`,
  so some rows had moved off page 0.
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
