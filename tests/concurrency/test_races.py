"""Races between in-process requests: threads released together, one connection each."""

import logging
import random
from collections import Counter

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session

from tests.concurrency.conftest import (
    assert_ledger_consistent,
    call_service,
    reset_data,
    run_concurrently,
)
from tests.conftest import (
    LOPEZ,
    NAIR,
    PINECREST,
    SAMPLE_REQUEST,
    add_firm,
    balances,
    bulk,
    payment_rows,
)

ROUNDS = 20


def _amount(cents: int) -> str:
    return f"{cents // 100}.{cents % 100:02d}"


@pytest.fixture
def service_errors(caplog: pytest.LogCaptureFixture) -> pytest.LogCaptureFixture:
    """Collects ERROR records of the service: a deadlock is logged there before it becomes busy."""
    caplog.set_level(logging.ERROR, logger="app.service")
    return caplog


def _error_messages(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.name == "app.service"]


def _c1_round(db: Session) -> None:
    reset_data(db)
    payer = add_firm(db, 10, 100_000)
    start = balances(db)
    payload = bulk(payer, (LOPEZ, "100"), (NAIR, "200"))

    outcomes = run_concurrently(lambda _: call_service(payload)[0], 10)

    assert Counter(outcomes) == {"created": 3, "insufficient_funds": 7}
    assert balances(db)[payer] == 10_000
    assert len(payment_rows(db)) == 6
    assert_ledger_consistent(db, start)


def test_c1_one_payer_many_batches(db):
    for _ in range(ROUNDS):
        _c1_round(db)


def test_c2_crossing_payments(db, service_errors):
    a = add_firm(db, 10, 10_000_000)
    b = add_firm(db, 11, 10_000_000)
    start = balances(db)
    # Interleaved, so both directions start at the barrier side by side.
    payloads = [bulk(a, (b, "1")), bulk(b, (a, "1"))] * 25

    for _ in range(ROUNDS):
        outcomes = run_concurrently(lambda i: call_service(payloads[i])[0], len(payloads))

        assert Counter(outcomes) == {"created": 50}, _error_messages(service_errors)
        current = balances(db)
        assert (current[a], current[b]) == (10_000_000, 10_000_000)
        assert_ledger_consistent(db, start)
    assert _error_messages(service_errors) == []


def _c3_batches(firms: list[str]) -> list[dict]:
    rng = random.Random(20261121)
    a, b, c = firms[:3]
    cycle = [bulk(a, (b, "100")), bulk(b, (c, "100")), bulk(c, (a, "100"))]
    batches = []
    for index in range(200):
        if index % 10 < 3:
            # Three legs of the cycle in every ten batches; with batch i on thread
            # i % 16, the legs at 0-2 and 10-12 start together at the barrier.
            batches.append(cycle[index % 10])
            continue
        payer = rng.choice(firms)
        others = [firm for firm in firms if firm != payer]
        payments = [
            (rng.choice(others), _amount(rng.randint(1, 20_000))) for _ in range(rng.randint(1, 4))
        ]
        batches.append(bulk(payer, *payments))
    return batches


def test_c3_cycle_and_random_mix(db, service_errors):
    firms = [add_firm(db, 10 + i, 50_000) for i in range(6)]
    start = balances(db)
    batches = _c3_batches(firms)
    threads = 16

    per_thread = run_concurrently(
        lambda i: [call_service(batch)[0] for batch in batches[i::threads]], threads
    )

    outcomes = Counter(outcome for results in per_thread for outcome in results)
    assert sum(outcomes.values()) == 200
    assert outcomes.keys() <= {"created", "insufficient_funds"}, _error_messages(service_errors)
    assert outcomes["created"] > 0
    assert len(payment_rows(db)) > 0
    assert_ledger_consistent(db, start)
    assert _error_messages(service_errors) == []


def test_c6_hot_payee(db):
    payers = [add_firm(db, 10 + i, 100_000) for i in range(20)]
    start = balances(db)

    outcomes = run_concurrently(lambda i: call_service(bulk(payers[i], (NAIR, "10")))[0], 20)

    assert outcomes == ["created"] * 20
    assert balances(db)[NAIR] == 200_000 + 20 * 1_000
    assert_ledger_consistent(db, start)


def _i5_round(db: Session) -> None:
    reset_data(db)
    start = balances(db)

    results = run_concurrently(lambda _: call_service(SAMPLE_REQUEST, key="i5"), 8)

    assert Counter(outcome for outcome, _ in results) == {"created": 1, "replayed": 7}
    bodies = [body for _, body in results]
    assert all(body == bodies[0] for body in bodies)
    assert len(payment_rows(db)) == 3
    assert balances(db)[PINECREST] == 3_674_875
    assert db.execute(text("SELECT count(*) FROM idempotency_keys")).scalar_one() == 1
    assert_ledger_consistent(db, start)


def test_i5_same_key_in_parallel(db):
    for _ in range(10):
        _i5_round(db)
