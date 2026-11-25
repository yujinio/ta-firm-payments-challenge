"""Load scenarios of spec section 13.5 (L1) against a running service.

Firm ids from 1000 belong to this script: it deletes them, with their payments, and inserts
its own firms before every run. After the run it checks the database and exits with 1 if a
balance is negative, the sum of balances moved, a load firm's balance is not explained by its
payments, the payment rows do not match the 201 responses, a request got a 500, or a request
got no response at all. 503 `busy` responses are counted but are not a failure.

    python scripts/load.py --scenario uniform --requests 2000 --concurrency 32
"""

import argparse
import json
import math
import queue
import random
import statistics
import sys
import time
from collections import Counter
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

import httpx2
from sqlalchemy import Connection, create_engine, text
from sqlalchemy.pool import NullPool

FIRST_FIRM_ID = 1000
FIRM_COUNT = 1100
START_BALANCE_CENTS = 1_000_000_000
BATCH_SIZE = 3
MAX_BATCH_SIZE = 1000
MAX_BATCH_BUDGET_MS = 250  # spec section 5.7: 5% of LOCK_TIMEOUT_MS
REQUEST_TIMEOUT_S = 30.0
SEED = 20261124
SCENARIOS = ("uniform", "hot-payer", "hot-payee", "max-batch")


@dataclass(frozen=True)
class Outcome:
    """What one request got: status, error code, the replica that answered, duration."""

    status: int | None  # None: no response
    error: str | None  # the error code of a non-201 response, or the exception name
    upstream: str | None
    duration_ms: float


def prepare_firms(conn: Connection) -> list[str]:
    """Remove leftovers of earlier runs, insert fresh firms and return their uuids by id."""
    first = {"first": FIRST_FIRM_ID}
    conn.execute(
        text("DELETE FROM payments WHERE payer_firm_id >= :first OR payee_firm_id >= :first"),
        first,
    )
    conn.execute(
        text(
            "DELETE FROM idempotency_keys"
            " WHERE payer_firm_uuid IN (SELECT uuid FROM firms WHERE id >= :first)"
        ),
        first,
    )
    conn.execute(text("DELETE FROM firms WHERE id >= :first"), first)
    conn.execute(
        text(
            "INSERT INTO firms (id, name, balance_cents, uuid)"
            " SELECT g, 'Load firm ' || g, :balance, gen_random_uuid()::text"
            " FROM generate_series(CAST(:first AS integer), CAST(:last AS integer)) AS g"
        ),
        {**first, "last": FIRST_FIRM_ID + FIRM_COUNT - 1, "balance": START_BALANCE_CENTS},
    )
    rows = conn.execute(text("SELECT uuid FROM firms WHERE id >= :first ORDER BY id"), first)
    return list(rows.scalars())


def balance_totals(conn: Connection) -> tuple[int, int]:
    """(firms with a negative balance, sum of all balances) over the whole table."""
    negative, total = conn.execute(
        text(
            "SELECT count(*) FILTER (WHERE balance_cents < 0), COALESCE(SUM(balance_cents), 0)"
            " FROM firms"
        )
    ).one()
    return int(negative), int(total)


def unexplained_balances(conn: Connection) -> int:
    """Load firms whose balance is not the start balance minus payments plus credits."""
    return int(
        conn.execute(
            text(
                "WITH paid AS (SELECT payer_firm_id AS id, SUM(amount_cents) AS cents"
                "  FROM payments WHERE payer_firm_id >= :first GROUP BY payer_firm_id),"
                " received AS (SELECT payee_firm_id AS id, SUM(amount_cents) AS cents"
                "  FROM payments WHERE payee_firm_id >= :first GROUP BY payee_firm_id)"
                " SELECT count(*) FROM firms f"
                " LEFT JOIN paid p ON p.id = f.id LEFT JOIN received r ON r.id = f.id"
                " WHERE f.id >= :first AND f.balance_cents"
                "  <> :start - COALESCE(p.cents, 0) + COALESCE(r.cents, 0)"
            ),
            {"first": FIRST_FIRM_ID, "start": START_BALANCE_CENTS},
        ).scalar_one()
    )


def load_payment_rows(conn: Connection) -> int:
    """Count the payment rows paid by load firms."""
    return int(
        conn.execute(
            text("SELECT count(*) FROM payments WHERE payer_firm_id >= :first"),
            {"first": FIRST_FIRM_ID},
        ).scalar_one()
    )


def random_amount(rng: random.Random) -> str:
    """Return a random amount from 0.01 to 10.00 as the API's decimal string."""
    cents = rng.randint(1, 1000)  # 0.01 to 10.00
    return f"{cents // 100}.{cents % 100:02d}"


def batch(payer: str, payees: Sequence[str], amounts: Sequence[str], label: str) -> bytes:
    """Encode one request body; every payment carries label as its description."""
    payload = {
        "payer_firm_uuid": payer,
        "payments": [
            {"amount": amount, "payee_firm_uuid": payee, "description": label}
            for payee, amount in zip(payees, amounts, strict=True)
        ],
    }
    return json.dumps(payload).encode()


def build_bodies(scenario: str, firms: list[str], requests: int) -> list[bytes]:
    """Request bodies, encoded before the clock starts; payees never include the payer."""
    label = f"load {scenario}"
    if scenario == "max-batch":
        payees = firms[1 : MAX_BATCH_SIZE + 1]
        body = batch(firms[0], payees, ["0.01"] * MAX_BATCH_SIZE, label)
        return [body] * requests

    rng = random.Random(SEED)
    hot, others = firms[0], firms[1:]
    bodies = []
    for _ in range(requests):
        if scenario == "uniform":
            payer, *payees = rng.sample(firms, 1 + BATCH_SIZE)
        elif scenario == "hot-payer":
            payer, payees = hot, rng.sample(others, BATCH_SIZE)
        else:  # hot-payee: the same payee in every batch
            payer, *rest = rng.sample(others, BATCH_SIZE)
            payees = [hot, *rest]
        amounts = [random_amount(rng) for _ in payees]
        bodies.append(batch(payer, payees, amounts, label))
    return bodies


def send_all(base_url: str, bodies: list[bytes], concurrency: int) -> tuple[list[Outcome], float]:
    """Post every body with `concurrency` threads; return the outcomes and the wall time."""
    clients: queue.SimpleQueue[httpx2.Client] = queue.SimpleQueue()
    for _ in range(concurrency):
        clients.put(httpx2.Client(base_url=base_url, timeout=REQUEST_TIMEOUT_S, trust_env=False))
    headers = {"Content-Type": "application/json"}

    def send(body: bytes) -> Outcome:
        """Post one body with a free client; an httpx2 error becomes an Outcome without status."""
        client = clients.get()
        started = time.perf_counter()
        try:
            response = client.post("/bulk-payments", content=body, headers=headers)
            elapsed_ms = (time.perf_counter() - started) * 1000
        except httpx2.HTTPError as exc:
            elapsed_ms = (time.perf_counter() - started) * 1000
            return Outcome(None, type(exc).__name__, None, elapsed_ms)
        finally:
            clients.put(client)
        error = None
        if response.status_code != 201:
            try:
                error = str(response.json()["error"]["code"])
            except ValueError, KeyError, TypeError:
                error = "unparsed body"
        return Outcome(response.status_code, error, response.headers.get("X-Upstream"), elapsed_ms)

    started = time.perf_counter()
    try:
        with ThreadPoolExecutor(max_workers=concurrency) as executor:
            outcomes = list(executor.map(send, bodies))
    finally:
        wall_s = time.perf_counter() - started
        for _ in range(concurrency):
            clients.get().close()
    return outcomes, wall_s


def percentile(sorted_values: list[float], p: float) -> float:
    """Nearest-rank percentile."""
    rank = max(1, math.ceil(p / 100 * len(sorted_values)))
    return sorted_values[rank - 1]


def counts(counter: Counter[str]) -> str:
    """Render "key: n" pairs sorted by key, or "-" when there are none."""
    return "  ".join(f"{key}: {n}" for key, n in sorted(counter.items(), key=str)) or "-"


def report(args: argparse.Namespace, outcomes: list[Outcome], wall_s: float) -> None:
    """Print throughput, latency percentiles and the status, error and upstream counts."""
    durations = sorted(outcome.duration_ms for outcome in outcomes)
    print(f"scenario      {args.scenario}")
    print(f"requests      {len(outcomes)} (concurrency {args.concurrency})")
    print(f"wall time     {wall_s:.3f} s")
    print(f"requests/s    {len(outcomes) / wall_s:.1f}")
    print(
        "latency ms    "
        + "  ".join(f"p{p} {percentile(durations, p):.1f}" for p in (50, 95, 99))
        + f"  max {durations[-1]:.1f}"
    )
    print(f"statuses      {counts(Counter(str(o.status or 'no response') for o in outcomes))}")
    print(f"errors        {counts(Counter(o.error for o in outcomes if o.error))}")
    print(f"upstreams     {counts(Counter(o.upstream for o in outcomes if o.upstream))}")
    if args.scenario == "max-batch":
        median = statistics.median(durations)
        print(
            f"max-batch     {MAX_BATCH_SIZE} payments per request: median {median:.1f} ms,"
            f" max {durations[-1]:.1f} ms; budget {MAX_BATCH_BUDGET_MS} ms (spec 5.7)"
        )
        if args.concurrency > 1:
            print(
                "              all requests share one payer, so these durations include"
                " waiting for its row lock"
            )


def main(argv: list[str] | None = None) -> int:
    """Prepare the load firms, run the scenario, check the database; return the exit code."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--base-url", default="http://localhost:8000")
    parser.add_argument(
        "--database-url",
        default="postgresql+psycopg://postgres:postgres@localhost:5432/firm_payments",
    )
    parser.add_argument("--scenario", choices=SCENARIOS, required=True)
    parser.add_argument("--requests", type=int, default=2000)
    parser.add_argument("--concurrency", type=int, default=32)
    args = parser.parse_args(argv)
    if args.requests < 1 or args.concurrency < 1:
        parser.error("--requests and --concurrency must be at least 1")

    engine = create_engine(args.database_url, poolclass=NullPool)
    try:
        with engine.begin() as conn:
            firms = prepare_firms(conn)
        with engine.connect() as conn:
            negative_before, total_before = balance_totals(conn)
        bodies = build_bodies(args.scenario, firms, args.requests)

        outcomes, wall_s = send_all(args.base_url, bodies, args.concurrency)

        with engine.connect() as conn:
            negative_after, total_after = balance_totals(conn)
            unexplained = unexplained_balances(conn)
            rows = load_payment_rows(conn)
    finally:
        engine.dispose()

    report(args, outcomes, wall_s)
    per_request = MAX_BATCH_SIZE if args.scenario == "max-batch" else BATCH_SIZE
    created = sum(1 for o in outcomes if o.status == 201)
    checks = [
        (
            "no negative balance (I-1)",
            negative_after == 0,
            f"{negative_after} negative (before: {negative_before})",
        ),
        (
            "sum of balances unchanged (I-2)",
            total_after == total_before,
            f"{total_before} -> {total_after} cents",
        ),
        ("load firms match their payments", unexplained == 0, f"{unexplained} mismatched"),
        (
            "payment rows match 201 responses",
            rows == created * per_request,
            f"{rows} rows, {created} x {per_request} expected",
        ),
        ("no 500 response", all(o.status != 500 for o in outcomes), ""),
        ("every request answered", all(o.status is not None for o in outcomes), ""),
    ]
    print("checks")
    for name, passed, detail in checks:
        print(f"  {'ok  ' if passed else 'FAIL'} {name}" + (f": {detail}" if detail else ""))
    failed = not all(passed for _, passed, _ in checks)
    print("result        " + ("FAIL" if failed else "PASS"))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
