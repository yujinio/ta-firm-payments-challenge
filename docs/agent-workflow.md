# Firm Payments: AI Agent Work Plan

- Date: 2026-11-21
- Related document: `docs/spec.md`

## How the models were chosen

The basis is Anthropic's recommendations (checked 21.11.2026), not measurements on this task:

- The platform documentation recommends starting with Opus 5.5 and using Fable 5.2 for tasks with heavy reasoning.
- The Claude Code usage guidance recommends Sonnet for most routine code and Opus for complex decisions, and the pairing of "plan with the stronger model, execute with the lighter one."

The allocation below is my judgment on that basis. If limits allow, steps 2 and 6 can go to Opus: the result will not be worse, only more expensive.

## General rules for all steps

1. Each step is a separate session. The session log is exported to `ai-logs/` without edits: the task requires this.
2. The agent does not deviate from the specification. If it finds a contradiction or gap, it stops and asks.
3. You read each step's output before moving to the next and record your own edits as separate commits. Reviewers evaluate exactly this involvement.
4. Code, comments, commit messages and the README are in English.

## Steps

| No. | Step | Model | Why |
|---|---|---|---|
| 0 | Design and specification | Fable 5.2 | Decisions with non-obvious consequences; done in this session |
| 1 | Implementation plan | Fable 5.2, this same session | The context of the decisions is already here; a new session would have to rebuild it |
| 2 | Scaffold: project, Docker, migrations, models, configuration | Sonnet 5.5 | The task is clearly described by sections 6, 7, 10, 11 |
| 3 | Core: validation, transaction, idempotency, errors | Opus 5.5 | The cost of error is highest here: the order of transaction steps and locks |
| 4 | Concurrency tests and load script | Opus 5.5 | Race tests are easy to write so that they pass by accident |
| 5 | Independent review | Fable 5.2, new session | The reviewer must not see how the code was written |
| 6 | README | You; Sonnet 5.5 for language editing | The README is read as your text; the substantive sections must be yours |

## Mapping to the implementation plan

The plan is `docs/plan.md`. Start every prompt for steps 2–4 with the line "Follow docs/plan.md, Tasks N–M, step by step", using this mapping:

| Workflow step | Plan tasks |
|---|---|
| 2. Scaffold | Tasks 1–2 |
| 3. Core | Tasks 3–6 |
| 4. Concurrency tests and load | Tasks 7–8 |
| 5–6. Review and README | Task 9 |

Where a prompt below and the plan disagree, the plan wins: it is newer and more specific.

## Prompts

### Step 1. Implementation plan

Done: see `docs/plan.md`.

### Step 2. Scaffold

```
Read docs/spec.md in full. Implement only the project scaffold per sections 6, 7, 10, 11:

1. A Python project with the dependencies from section 6, versions pinned by a lock file.
2. docker-compose.yml: the run stand from spec section 5.6 (PostgreSQL, a one-shot migration service, three application replicas, an nginx balancer). One command brings everything up.
3. An Alembic migration with the three tables from section 7; DDL verbatim from the specification.
4. ORM models, configuration via environment variables (section 10), engine and session factory.
5. scripts/seed.sql with the INSERTs from the task, unchanged.
6. GET /healthz with a database connection check.
7. pytest configuration connecting to a test PostgreSQL database, with table cleanup between tests.
8. ruff, mypy, and one command that runs all checks.

Do not implement the payments endpoint or business logic.
Do not add anything that is not in the specification. If the specification is contradictory or incomplete, stop and ask.
Code, comments and commit messages are in English. Make small commits, one logical change each.
At the end, show: the output of the run command, the output of the checks, and the migration apply and rollback on an empty database.
```

### Step 3. Core

```
Read docs/spec.md in full, especially sections 8 and 9. The scaffold is already done.
Implement POST /bulk-payments strictly per section 8.

Work test-first: a failing test first, then minimal code.
1. Amount parsing and request hash: tests U1–U5, then code.
2. Request and response schemas, single error format: tests A9, A12.
3. Transaction per section 8.2 without idempotency: tests A1–A8, A10, A11, A13.
4. Idempotency: tests I1–I4, I6–I8.

Mandatory requirements:
- Transaction steps follow the order of section 8.2. Key insert first; firm locking in one query with ORDER BY id and FOR NO KEY UPDATE.
- Firms are not read from the database before the locking query.
- Nothing inside the transaction except database queries.
- float is not used anywhere.
- api.py contains no SQL; service.py knows nothing about HTTP.

Do not write the concurrency tests (C1–C7, I5) or the load script: that is the next step.
If the specification is contradictory, incomplete, or you consider a decision in it wrong, stop and explain. Do not silently fix it.
Code, comments and commit messages are in English. Small commits.
At the end, show the full output of the tests and static checks.
```

### Step 4. Concurrency tests and load

```
Read docs/spec.md, sections 9 and 13. The endpoint is implemented and the contract tests pass.
Write tests C1–C7 and I5, and the script scripts/load.py (scenario L1).

Requirements for the tests:
- Real PostgreSQL only.
- Threads start simultaneously via threading.Barrier; each thread has its own connection.
- After each test, invariants I-1 and I-2 from section 9.1 are checked.
- C4 is deterministic: a separate connection locks a firm's row; the test confirms the request waits, then releases the lock and checks the result.
- C7 goes through HTTP against the service running in several processes.
- Each concurrency scenario is repeated many times.

Verifying the tests themselves is mandatory:
Run the three mutations from Task 7, Step 4 of the plan (M1: remove the locking clause; M2: remove ORDER BY; M3: split the lock query in two) and show which tests fail under each. Restore the code after each change. If the tests stay green under a mutation, they prove nothing: rework them.

scripts/load.py: the four scenarios from L1, including max-batch, run through the balancer; output: requests per second and p50/p95/p99; after the run, invariant checks. Then stand check S3.
Do not change the service code. If you find a defect, stop and describe it with a reproduction.
At the end, show the test output, the result of the check with the lock disabled, and the load run results.
```

### Step 5. Independent review

New session. The agent receives only the repository and the specification, with no history of previous sessions.

```
You are reviewing someone else's pull request. Read docs/spec.md, then all the code and tests.
Do not fix anything yourself.

Check in order:
1. Each step of section 8.2: is it implemented, and in that order? Give file and line.
2. Each invariant in section 9.1: how it is ensured in the code and which test verifies it.
3. Each row in the tables of sections 13.2–13.5: does a test exist, and does it verify what is claimed?
4. The five claims at the end of section 17 that the tests must re-confirm: do the tests confirm them?
5. Divergences between the code and the specification in both directions: what is missing and what is extra.
6. Run the acceptance procedure from section 14.2 on a clean clone, strictly following the README.

Separately, try to find an interleaving of two or three requests that violates any invariant in section 9.1. If you find one, describe it step by step.

Report format: a list of findings in descending severity; for each, the location in the code, what is wrong, and how to reproduce it. Then, what was verified and found correct. No general praise.
```

### Step 6. README

You write the substantive sections: approach, encountered problems, assumptions, improvements. Sources: sections 4, 5 and 15 of the specification, and your notes from the work. A model is used only for language editing:

```
Below is my draft README in English. Fix grammar and style, preserving the structure, meaning and all technical claims. Do not add or remove anything substantive. Show the changes as a list.
```

The answers to the two questions at the end of the README (time spent and self-assessment) are yours alone.
