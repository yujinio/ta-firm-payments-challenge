# Independent review session

- Session: `a81e543f-a226-42f0-8cc1-88cfc8a1baeb`
- Tool: Claude Code (CLI), in a separate clean clone of the repository
- Model: Claude Fable 5.2
- Date: 2026-11-24, 15:06 to 16:14 (UTC)
- Workflow step: 5, the independent review. The session saw only the repository, not the history of the implementation.

The dialogue only, translated from Russian where needed; see `README.md` in this folder for what was left out. The review prompt was written in English and is quoted as sent.


## 1. 2026-11-24 15:06 UTC

**User**:

`/model` (slash command) _(command output: Set model to `Fable 5.2` and saved as your default for new sessions)_

## 2. 2026-11-24 15:06 UTC

**User**:

> **Pasted text:**
>
> You are reviewing someone else's pull request. Read docs/spec.md, then all the code and tests.
> Do not fix anything yourself.
>
> Check in order:
> 1. Each step of section 8.2: is it implemented, and in that order? Give file and line.
> 2. Each invariant in section 9.1: how it is ensured in the code and which test verifies it.
> 3. Each row in the tables of sections 13.2–13.5: does a test exist, and does it verify what is claimed?
> 4. The five claims at the end of section 17 that the tests must re-confirm: do the tests confirm them?
> 5. Divergences between the code and the specification in both directions: what is missing and what is extra.
> 6. Run the acceptance procedure from section 14.2 on a clean clone, strictly following the README.
>
> Separately, try to find an interleaving of two or three requests that violates any invariant in section 9.1. If you find one, describe it step by step.
>
> Report format: a list of findings in descending severity; for each, the location in the code, what is wrong, and how to reproduce it. Then, what was verified and found correct. No general praise.

**Claude:**

# Review of PR `feat/bulk-payment-service` (8c1107e)

I could not violate the 9.1 invariants, and the core of the transaction matches the specification. The main remarks concern the delivery (empty `ai-logs/`, cloning per the README), the reliability of the mutation check, and behavior when threads are saturated.

I ran the acceptance on Podman 5.8.2 + Compose v5.1.3, not on Docker Engine. I did not run the load scenario L1 (`make load`), I only read `scripts/load.py`.

## Findings in descending severity

### 1. `ai-logs/` is empty
- **Where:** `ai-logs/.gitkeep` is the only file; `README.md:485-488`, spec 14.3, R11.
- **What is wrong:** The README claims that the directory contains the working sessions with an index of tools and models. There are no logs and no index in the branch, so a mandatory part of the delivery is missing.
- **How to reproduce:** `git ls-files ai-logs`.

### 2. A clean clone does not start per the README
- **Where:** `README.md:29-31`.
- **What is wrong:** `git clone` gives the `main` branch (849732c), which contains only `docs/`. Steps 1–2 of section 14.2 "strictly per the README" do not pass until a merge or a manual `git checkout feat/bulk-payment-service`.
- **How to reproduce:** the commands from the README in sequence; `make up` answers `No rule to make target 'up'`.

### 3. Mutation M2 (no `ORDER BY`) is not always caught by the behavioral tests
- **Where:** `tests/concurrency/test_races.py:102` (C3), `:65` (C2); `app/service.py:197`; spec 13.7, `README.md:187`.
- **What is wrong:** over 10 runs C3 failed 9 times, and once all behavioral tests stayed green. The spec and README say "3 out of 3".
- C2 never catches M2 (this is acknowledged in the spec). Only `test_statement_order` (`test_locks.py:220`) catches it deterministically, and that is a comparison of the SQL text with a regular expression, not behavior.
- This conflicts with principle 13.1 "tests do not rely on chance" for the first claim of section 17.
- Incidentally: `test_c7_crossing_over_http` (two firms) failed under M2 in 3 runs out of 10. The spec 5.4/8.3 claim that the two-firm case without `ORDER BY` causes no failures is not confirmed over HTTP.
- **How to reproduce:** delete line `service.py:197`, run `uv run --locked pytest tests/concurrency/test_races.py tests/concurrency/test_multiprocess.py` 10 times.

### 4. A thread limit equal to the pool size removes the upper bound on waiting
- **Where:** `app/main.py:78`, `app/db.py:22`, `tests/concurrency/test_locks.py:183-203`.
- **What is wrong:** a request waits for a free thread with no timeout and no 503, and only then does the `lock_timeout` countdown start.
- **Measured:** an external row lock held for 14 s and 45 parallel requests to that firm. 43 responses of 503 arrived after 5.0–10.0 s, and two responses of 201 after 13.06 s. `/healthz` once answered in 2.97 s.
- **Consequence for the pool 503:** with default settings the branch "no connection in the pool → 503" (table 8.2) is apparently unreachable over HTTP: every connection holder also holds a thread. C9 checks only the service function on its own engine; no test checks that `DB_POOL_TIMEOUT_S` is passed to the application's engine.
- **Conclusion without measurement:** under sustained saturation longer than 60 s, nginx will return 504, and the request may be executed later. Limitation 13 in the spec describes only waiting for locks, not the thread queue.
- **How to reproduce:** `BEGIN; SELECT 1 FROM firms WHERE id=2 FOR UPDATE; SELECT pg_sleep(14);` in psql, with 45 POSTs with Lopez among the recipients in parallel.

### 5. `/healthz` with an unavailable database does not answer in the unified error format
- **Where:** `app/api.py:27-28`; the exception is described only in the docstring of `app/errors.py:23`.
- **What is wrong:** the response `503 {"status":"unavailable"}` contradicts A12 "all 4xx and 5xx in a unified format". There is no test for a 503 from `/healthz`.
- A12 also does not cover the code `http_error`; the `idempotency_key_reuse` format is checked only by the `code` field in I2.
- **How to reproduce:** `docker compose stop db`, then `curl localhost:8000/healthz`.

### 6. The payment count limit is checked after full validation of the body
- **Where:** `app/service.py:77-80` versus `app/schemas.py:69`.
- **What is wrong:** 60,000 payments with an invalid amount (5.76 MB) give a 422 with an 8.99 MB body, with one `details` entry per payment. Directly to a replica there is no body limit at all.
- **How to reproduce:** POST a body with 60,000 elements `{"amount":"x",...}`.

### 7. Some claims in section 17 are not re-verified by tests
- **Where:** spec 17, the phrase "the project tests re-verify every item".
- **What is wrong:** there are no tests for the following:
  - the syntax error of `SET LOCAL` with a parameter;
  - that `FOR NO KEY UPDATE` does not block an insert into `payments`, while `FOR UPDATE` does (a replacement of `key_share=True` would be caught only by the regular expression);
  - key reordering in `JSONB`;
  - the order of ids for 1,000 rows;
  - the impossibility of changing the thread limit from a worker thread.

### 8. Extras relative to the specification
- `/docs`, `/redoc`, `/openapi.json` are available through the load balancer (200), FastAPI defaults, `app/main.py:205`.
- The ban on lone surrogates in `description` (`app/schemas.py:44-49`) is in the README but not in the rules table 8.1.
- Any `HTTPException(400)` turns into a `validation_error` "body is not JSON" (`app/main.py:187-192`).
- `POST /bulk-payments/` answers 307; this is not in the response table, and the request does not get into the log.
- The log record has a `status` field that is not in spec 12.

### 9. The order of acceptance steps in the README differs from 14.2
In the README, the S2/S3 checks come before the tests and a step on idempotency is added. In substance, all the steps of 14.2 are present.

## Search for a violating interleaving

No interleaving of two or three requests that violates a 9.1 invariant was found.

- **Wait-for graph:** a transaction holding a firm lock has already passed the key step and cannot wait for the key. Firms are taken with a single query by `id`. No cycle forms.
- **Scenario A on the stand:** R1 (key k, body X) waits for the firm, R2 (k, body Y) and R3 (k, X) wait for the key. Result: R1 — 201, R3 — replay, R2 — 422 `idempotency_key_reuse`; one execution, three payment rows.
- **Scenario B:** a lock held longer than `lock_timeout`. R1 — 503, R2 (Y) executes, the replay R3 (X) — 422. One execution per key, the balance adds up. A client that received 503 may lose the key to another body — this matches 5.3.
- **Saturation (finding 4):** after 45 requests, 2 payments, balances 4999998 / 50002, the total did not change.

## Verified and found correct

### 1. Steps of 8.2 — all implemented in the stated order

| Step | Location |
|---|---|
| Before transaction 1: parsing and validation | `app/schemas.py:15-69`, `app/service.py:77-95` |
| 2: cents and total | `app/service.py:98-114` |
| 3: sums per recipient | `app/service.py:116-118` |
| 4: set of UUIDs | `app/service.py:120` |
| 5: request hash | `app/service.py:122-126`, `app/idempotency.py:15-26` |
| Transaction 1: `set_config` | `app/service.py:179-182` |
| 2: key insert, replay or 422 | `app/service.py:184-191`, `app/idempotency.py:39-61` |
| 3: locking query | `app/service.py:194-200` |
| 4: `firm_not_found` | `app/service.py:203-205` |
| 5: `insufficient_funds` | `app/service.py:207-209` |
| 6: `balance_overflow` | `app/service.py:211-213` |
| 7: payments insert | `app/service.py:215-225` |
| 8: balance update | `app/service.py:227-229` (goes to the database at `:247` or on commit) |
| 9: response record | `app/service.py:245-248`, `app/idempotency.py:64-70` |
| 10: commit and 201 | `app/service.py:158`, `app/api.py:56` |
| Database errors: 55P03, 40P01, pool, other | `app/service.py:160-171`, `app/main.py:197-200` |

The order of SQL statements is fixed by the test `test_locks.py:271`.

### 2. Invariants 9.1

| No. | How it is ensured | Test |
|---|---|---|
| I-1 | The check at `service.py:208` under the lock `:194-200` | C1 `test_races.py:60`, C4 `test_locks.py:68`, `assert_ledger_consistent` (`tests/concurrency/conftest.py:80`); M1 makes 7 behavioral tests fail for me in 2 runs out of 2 |
| I-2 | Total and credits from the same items (`:111`, `:116-118`), one transaction | `assert_ledger_consistent` in C1–C3, C6, C7, I5 |
| I-3 | `:215-229` in one transaction | Reconciliation per firm "balance = start − paid out + received"; no direct per-batch check |
| I-4 | `with session.begin()` `:158` | A2, A5, A11, `tests/db/test_engine.py:12` (failure on insert), C5; the tests do not model a failure at steps 8–9 |
| I-5 | Unique index and `ON CONFLICT` in the same transaction | I1, I5 `test_races.py:147`, `test_locks.py:141` and `:159`, C7, S3 on the stand |
| I-6 | Engine and settings per process, no shared state | C7 (4 uvicorn processes), S2/S3 |

### 3. Tables 13.2–13.5 — there is a test for every row

| Rows | Location | Note |
|---|---|---|
| U1–U3, U5 | `tests/unit/test_amount.py:6`, `:21`, `:39`, `:50` | "Number instead of string" and `null` from U2 are in `test_schemas.py:35-36` |
| U4 | `tests/unit/test_request_hash.py:16`, `:22` | Matches |
| A1–A8, A10, A11, A13, A15, A16 | `tests/api/test_bulk_payments.py:22-158` | A13 checks only the uniqueness of ids; the order by id is visible in A8 |
| A9, A14 | `tests/api/test_validation.py:59`, `:83` | All listed cases are present |
| A12 | `tests/api/test_validation.py:156-219` | Incomplete, see finding 5 |
| I1–I4, I6–I9 | `tests/api/test_idempotency.py:27-138` | I7 checks being filled in on a single row |
| I5 | `tests/concurrency/test_races.py:147` | 8 threads × 10 rounds, plus over HTTP in C7 |
| C1–C3, C6 | `tests/concurrency/test_races.py:60`, `:65`, `:102`, `:121` | C2/C3 — see finding 3 |
| C4, C5, C8 | `tests/concurrency/test_locks.py:68`, `:88`, `:112`, `:124` | SQLSTATE 55P03 is checked explicitly |
| C7 | `tests/concurrency/test_multiprocess.py:127`, `:144`, `:163` | Matches |
| C9 | `tests/concurrency/test_locks.py:183` | Service level only, see finding 4 |
| L1 | `scripts/load.py` | Checks for I-1, I-2 and absence of 500 are in the code; I did not run it |

### 4. The five claims at the end of section 17

| Claim | Confirmed by tests? |
|---|---|
| Order of taking locks | Yes, with the caveat of finding 3; M3 is caught by C2 and C3 in 2 runs out of 2 |
| `ON CONFLICT DO NOTHING` after waiting | Yes: I5 and the deterministic `test_locks.py:141`, `:159` |
| `lock_timeout` on waiting for a unique index, 55P03 | Yes: C5, C8 |
| 422 on invalid JSON and non-objects | Yes: A9, A14 |
| Thread limit | Partially: `test_locks.py:210` checks the values 40 and 10, behavior under load is not checked |

### 5. Divergences code ↔ spec
The schema (7.1, 7.2), configuration (10), contract and error codes (8.1), nginx (5.6) and the startup revision check match the specification. I did not find anything the spec requires that is missing in the code; the extras are listed in finding 8.

### 6. Acceptance 14.2 (after a manual checkout of the branch)
- `make up`: `db` and `lb` are running, three `app`, `migrate` finished with code 0.
- The example from p. 4: 201, balances 3674875 / 170075 / 1405050.
- Three more times: 201, 201, 422 `insufficient_funds`.
- `make reset` and the request from p. 3: 201, balances 4849925 / 170075 / 230000.
- S2: three `X-Upstream` values (10 / 11 / 9 out of 30).
- S3: five 201s, four replays, one request took 2.01 s, the balance changed once.
- `make check`: code 0, 159 tests (64 / 14 / 63 / 18), ruff and mypy clean.
- Database stopped: POST answers 500 `internal_error` (per table 8.2); after the database was started, the replicas recovered on their own.

I removed my stand (`docker compose down -v`). The image `firm-payments:latest` was rebuilt in the process. The clone remained at `/private/tmp/claude-501/-Users-<user>-Development-taxdome-ta-firm-payments-review/a81e543f-a226-42f0-8cc1-88cfc8a1baeb/scratchpad/accept/ta-firm-payments-challenge`.

_Actions: Bash ×19, Read ×6._

_[The context was compacted here; the session continued from a summary.]_
