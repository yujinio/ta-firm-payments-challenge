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

`test_c2_crossing_payments` passed under M2 in all three runs. That is 3,000 crossing
transactions with no deadlock and no `busy`, whereas the design probe saw 80 deadlocks in 2,400
(spec section 17). The likely reason is that the ORM flushes the balance updates as one
executemany in primary-key order: a request from Nair (id 3) to Pinecrest (id 1) and Lopez (id 2)
updates ids 1, 2, 3 in that order (checked). So in C2 every transaction writes the new version
of A before the new version of B, and the sequential scan of the lock query (the plan without
`ORDER BY` is `LockRows` over `Seq Scan`) keeps finding the two rows in the same order. Why the
probe saw deadlocks is not verified: the probe is not in the repository. One possibility is that
it updated payer then payee, which would alternate the heap order. C3 touches overlapping
subsets of six firms, so heap order and id order drift apart, and different snapshots see
different orders. Spec 13.7 expects "C2 or C3" to fail; C3 is the test that catches M2 here.

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
| Lock acquisition order for `ORDER BY ... FOR NO KEY UPDATE` | `test_c2_crossing_payments` (20 rounds × 50 crossing transactions), `test_c3_cycle_and_random_mix` (200 batches, cycle A→B→C→A, seed 20261121, 16 threads), mutations M2 and M3, a manual `EXPLAIN` (not a test) | Confirmed. With `ORDER BY id`, C2 and C3 had no `busy` outcome and logged no deadlock in any run. Without it, C3 deadlocks (M2, 3 of 3 runs). On PostgreSQL 18.6, a hand-written equivalent of the lock query (`SELECT id, uuid, balance_cents FROM firms WHERE uuid IN (...) ORDER BY id FOR NO KEY UPDATE`), run once on the test database with the three seed rows, plans as `LockRows` above `Sort (Sort Key: id)` above `Seq Scan on firms`. That is the plan the spec describes. Caveat: the crossing workload alone (C2) does not deadlock without `ORDER BY` here (see M2). |
| `ON CONFLICT DO NOTHING` after waiting on another transaction | `test_i5_same_key_in_parallel` (10 rounds × 8 threads, one key), `test_key_wait_then_commit_replays`, `test_key_wait_then_rollback_executes` | Confirmed. Each I5 round gave exactly 1 `created` and 7 `replayed`, identical bodies, 3 payment rows, Pinecrest 3 674 875 and one key row. With another transaction holding the key row, the request waits. After that transaction commits, the request returns the committed response and changes nothing. After it rolls back, the request executes and claims the key. |
| `lock_timeout` bounds the unique-index wait; error code 55P03 | `test_key_wait_respects_lock_timeout` (C8), `test_c5_lock_timeout`, `test_c5_http_503` | Confirmed. With an uncommitted key row held elsewhere and `lock_timeout_ms=200`, the request returns `busy` after at least 200 ms, and the cause is SQLSTATE `55P03`. The row-lock wait (C5) behaves the same way. Over HTTP the response is 503 `busy` with `Retry-After: 1`. Nothing changed in any of these cases. |
| 422 for invalid JSON and non-object bodies in FastAPI 0.165.0 | `tests/api/test_validation.py::test_non_object_bodies` (`[]`, `"x"`, `null`, `123`, invalid JSON, empty body, JSON sent as `text/plain`, invalid UTF-8) and `::test_a9_schema_violations` | Confirmed. Every case gives 422 `validation_error`. |
| Thread limit for synchronous endpoints (spec section 10) | `test_thread_limit_matches_pool_size` | Confirmed on AnyIO 4.16.0. A fresh event loop reports 40 tokens. In the application's loop after the lifespan has run, the limit is 10, which is `DB_POOL_SIZE`. |

Also observed: with the only pooled connection checked out, `execute_bulk_payment` raises
`ServiceBusy` whose cause is `sqlalchemy.exc.TimeoutError` (`test_pool_exhaustion_is_busy`,
C9). This matches the spec 17 note that pool exhaustion is not a `DBAPIError`.
