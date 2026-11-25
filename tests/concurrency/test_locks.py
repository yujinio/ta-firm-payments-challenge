"""Deterministic lock behavior: waits, timeouts, pool exhaustion and statement order."""

import re
import time
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager

import anyio
import anyio.to_thread
import pytest
from sqlalchemy import Connection, create_engine, event, text
from sqlalchemy.exc import TimeoutError as PoolTimeoutError
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import get_engine
from app.errors import ServiceBusy
from app.main import app
from app.schemas import BulkPaymentRequest
from app.service import build_command, execute_bulk_payment
from tests.concurrency.conftest import (
    assert_ledger_consistent,
    call_service,
    run_service,
    wait_until_blocked,
)
from tests.conftest import (
    LOPEZ,
    NAIR,
    PINECREST,
    SAMPLE_REQUEST,
    balances,
    bulk,
    payment_rows,
)

LOCK_LOPEZ = "SELECT 1 FROM firms WHERE id = 2 FOR UPDATE"
LOPEZ_PAYS_NAIR = bulk(LOPEZ, (NAIR, "400"))
SHORT_TIMEOUT_MS = 200
LOCK_NOT_AVAILABLE = "55P03"


@contextmanager
def other_transaction(statement: str, **params: object) -> Iterator[Connection]:
    """Another session's open transaction that has run statement; rolled back on exit
    unless the caller commits first."""
    with get_engine().connect() as conn:
        conn.execute(text(statement), params)
        yield conn


INSERT_PENDING_KEY = (
    "INSERT INTO idempotency_keys (payer_firm_uuid, key, request_hash)"
    " VALUES (:payer, :key, :request_hash)"
)


def key_rows(db: Session) -> list[tuple[str, object]]:
    rows = db.execute(text("SELECT key, response_body FROM idempotency_keys ORDER BY id")).all()
    return [(key, body) for key, body in rows]


def busy_sqlstate(error: ServiceBusy) -> str | None:
    return getattr(getattr(error.__cause__, "orig", None), "sqlstate", None)


def test_c4_request_waits_for_row_lock(db):
    start = balances(db)
    # The thread is joined only after the other transaction has ended, so a failed
    # assertion cannot leave it waiting for a lock nobody releases.
    with ThreadPoolExecutor(max_workers=1) as executor:
        with other_transaction(LOCK_LOPEZ) as conn:
            future = executor.submit(call_service, LOPEZ_PAYS_NAIR)
            wait_until_blocked(db)
            assert not future.done()
            conn.execute(text("UPDATE firms SET balance_cents = 30000 WHERE id = 2"))
            conn.commit()
        result = future.result(timeout=10)

    # The lock query returned the balance committed while it waited: 300.00 < 400.00.
    assert result == ("insufficient_funds", None)
    assert balances(db)[LOPEZ] == 30_000
    assert payment_rows(db) == []
    assert_ledger_consistent(db, {**start, LOPEZ: 30_000})


def test_c5_lock_timeout(db):
    before = balances(db)
    with other_transaction(LOCK_LOPEZ):
        started = time.monotonic()
        assert call_service(LOPEZ_PAYS_NAIR, lock_timeout_ms=SHORT_TIMEOUT_MS) == ("busy", None)
        assert time.monotonic() - started >= SHORT_TIMEOUT_MS / 1000
        with pytest.raises(ServiceBusy) as excinfo:
            run_service(LOPEZ_PAYS_NAIR, lock_timeout_ms=SHORT_TIMEOUT_MS)

    assert busy_sqlstate(excinfo.value) == LOCK_NOT_AVAILABLE
    assert balances(db) == before
    assert payment_rows(db) == []


@pytest.fixture
def short_lock_timeout() -> Iterator[None]:
    settings = get_settings().model_copy(update={"lock_timeout_ms": SHORT_TIMEOUT_MS})
    app.dependency_overrides[get_settings] = lambda: settings
    try:
        yield
    finally:
        app.dependency_overrides.pop(get_settings, None)


def test_c5_http_503(client, db, short_lock_timeout):
    before = balances(db)
    with other_transaction(LOCK_LOPEZ):
        r = client.post("/bulk-payments", json=LOPEZ_PAYS_NAIR)

    assert r.status_code == 503
    assert r.json()["error"]["code"] == "busy"
    assert r.headers["Retry-After"] == "1"
    assert balances(db) == before
    assert payment_rows(db) == []


def test_key_wait_respects_lock_timeout(db):
    """C8: the wait on the unique index of idempotency_keys is bounded by lock_timeout."""
    before = balances(db)
    with other_transaction(INSERT_PENDING_KEY, payer=PINECREST, key="k", request_hash="x"):
        started = time.monotonic()
        result = call_service(SAMPLE_REQUEST, key="k", lock_timeout_ms=SHORT_TIMEOUT_MS)
        assert result == ("busy", None)
        assert time.monotonic() - started >= SHORT_TIMEOUT_MS / 1000
        with pytest.raises(ServiceBusy) as excinfo:
            run_service(SAMPLE_REQUEST, key="k", lock_timeout_ms=SHORT_TIMEOUT_MS)

    assert busy_sqlstate(excinfo.value) == LOCK_NOT_AVAILABLE
    assert balances(db) == before
    assert payment_rows(db) == []
    assert key_rows(db) == []


def test_key_wait_then_rollback_executes(db):
    """Spec 8.3: if the transaction holding the key rolls back, the waiting request runs."""
    start = balances(db)
    with ThreadPoolExecutor(max_workers=1) as executor:
        with other_transaction(INSERT_PENDING_KEY, payer=PINECREST, key="k", request_hash="x"):
            future = executor.submit(call_service, SAMPLE_REQUEST, "k")
            wait_until_blocked(db)
            assert not future.done()
        # Leaving the block rolled the other transaction back.
        outcome, body = future.result(timeout=10)

    assert outcome == "created"
    assert balances(db)[PINECREST] == 3_674_875
    assert len(payment_rows(db)) == 3
    assert key_rows(db) == [("k", body)]
    assert_ledger_consistent(db, start)


def test_key_wait_then_commit_replays(db: Session) -> None:
    """Spec 8.3: if the transaction holding the key commits, the waiting request gets
    the stored response and changes nothing."""
    before = balances(db)
    command = build_command(BulkPaymentRequest.model_validate(SAMPLE_REQUEST), "k", get_settings())
    with ThreadPoolExecutor(max_workers=1) as executor:
        with other_transaction(
            INSERT_PENDING_KEY, payer=PINECREST, key="k", request_hash=command.request_hash
        ) as conn:
            future = executor.submit(call_service, SAMPLE_REQUEST, "k")
            wait_until_blocked(db)
            assert not future.done()
            conn.execute(
                text("UPDATE idempotency_keys SET response_body = :body WHERE key = 'k'"),
                {"body": '{"stored": true}'},
            )
            conn.commit()
        result = future.result(timeout=10)

    assert result == ("replayed", {"stored": True})
    assert balances(db) == before
    assert payment_rows(db) == []


def test_pool_exhaustion_is_busy(db: Session) -> None:
    """C9: no free connection within the pool timeout is busy, not an internal error."""
    before = balances(db)
    engine = create_engine(
        get_settings().database_url,
        pool_size=1,
        max_overflow=0,
        pool_timeout=0.2,
        hide_parameters=True,
    )
    command = build_command(BulkPaymentRequest.model_validate(SAMPLE_REQUEST), None, get_settings())
    try:
        with engine.connect(), Session(engine) as session:
            with pytest.raises(ServiceBusy) as excinfo:
                execute_bulk_payment(session, command, 5000)
    finally:
        engine.dispose()

    assert isinstance(excinfo.value.__cause__, PoolTimeoutError)
    assert balances(db) == before
    assert payment_rows(db) == []


async def _thread_limit() -> float:
    return anyio.to_thread.current_default_thread_limiter().total_tokens


def test_thread_limit_matches_pool_size(client):
    """Spec 10: AnyIO allows 40 threads by default; the lifespan lowers it to the pool size."""
    assert anyio.run(_thread_limit) == 40
    assert client.portal.call(_thread_limit) == get_settings().db_pool_size


# Spec 8.2, steps 1-3 and 7-9: one label per statement the service sends.
STATEMENT_LABELS = {
    "set_config": r"SELECT set_config\('lock_timeout', ",
    "claim_key": r"INSERT INTO idempotency_keys .* ON CONFLICT \(payer_firm_uuid, key\) DO NOTHING",
    "lock_firms": r"SELECT firms\.id, .* FROM firms WHERE .* ORDER BY firms\.id FOR NO KEY UPDATE$",
    "insert_payments": r"INSERT INTO payments ",
    "update_firms": r"UPDATE firms SET balance_cents=",
    "store_response": r"UPDATE idempotency_keys SET response_body=",
}


def _label(statement: str) -> str:
    flat = " ".join(statement.split())
    for label, pattern in STATEMENT_LABELS.items():
        if re.match(pattern, flat):
            return label
    return f"other: {flat[:80]}"


@pytest.fixture
def statements() -> Iterator[tuple[Session, list[str]]]:
    """A session on an engine of its own that records the label of every statement."""
    engine = create_engine(get_settings().database_url, hide_parameters=True)
    seen: list[str] = []

    def record(conn, cursor, statement, parameters, context, executemany) -> None:
        seen.append(_label(statement))

    event.listen(engine, "before_cursor_execute", record)
    try:
        with Session(engine, expire_on_commit=False) as session:
            yield session, seen
    finally:
        event.remove(engine, "before_cursor_execute", record)
        engine.dispose()


@pytest.mark.parametrize(
    ("key", "expected"),
    [
        (
            "order-1",
            [
                "set_config",
                "claim_key",
                "lock_firms",
                "insert_payments",
                "update_firms",
                "store_response",
            ],
        ),
        (None, ["set_config", "lock_firms", "insert_payments", "update_firms"]),
    ],
    ids=["with_key", "without_key"],
)
def test_statement_order(
    statements: tuple[Session, list[str]], key: str | None, expected: list[str]
) -> None:
    """Pins spec 8.2: the key is claimed before any firm is read, firms are read once
    and locked in id order, and the response is stored after payments and balances."""
    session, seen = statements
    command = build_command(BulkPaymentRequest.model_validate(SAMPLE_REQUEST), key, get_settings())

    result = execute_bulk_payment(session, command, 5000)

    assert not result.replayed
    assert seen == expected
