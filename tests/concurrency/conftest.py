import threading
import time
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import pytest
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.orm import Session

from app.config import get_settings
from app.errors import DomainError
from app.schemas import BulkPaymentRequest, BulkPaymentResponse
from app.service import BulkPaymentResult, build_command, execute_bulk_payment
from tests.conftest import SEED_SQL

# The largest thread count any concurrency test uses. Every thread gets its own
# pooled connection without waiting, so a pool wait never shows up as "busy".
MAX_THREADS = 50
THREAD_TIMEOUT_S = 60.0

SERVICE_ENGINE: Engine = create_engine(
    get_settings().database_url,
    pool_size=MAX_THREADS,
    max_overflow=0,
    pool_timeout=30,
    hide_parameters=True,
)


@pytest.fixture(scope="module", autouse=True)
def _close_service_connections() -> Iterator[None]:
    """Do not keep MAX_THREADS idle connections open beyond the module that used them."""
    yield
    SERVICE_ENGINE.dispose()


def run_concurrently[T](fn: Callable[[int], T], n: int) -> list[T]:
    """Run fn(0) .. fn(n - 1) on n threads released together; re-raise the first exception."""
    barrier = threading.Barrier(n, timeout=THREAD_TIMEOUT_S)

    def start(index: int) -> T:
        """Wait until all n threads are ready, then run fn(index)."""
        barrier.wait()
        return fn(index)

    with ThreadPoolExecutor(max_workers=n) as executor:
        futures = [executor.submit(start, index) for index in range(n)]
        return [future.result(timeout=THREAD_TIMEOUT_S) for future in futures]


# Any: payload is a raw JSON request body; the service validates it.
def run_service(
    payload: dict[str, Any], key: str | None = None, lock_timeout_ms: int = 5000
) -> BulkPaymentResult:
    """Run one request through the service on a connection of its own; errors propagate."""
    command = build_command(BulkPaymentRequest.model_validate(payload), key, get_settings())
    with Session(SERVICE_ENGINE, expire_on_commit=False) as session:
        return execute_bulk_payment(session, command, lock_timeout_ms)


def call_service(
    payload: dict[str, Any], key: str | None = None, lock_timeout_ms: int = 5000
) -> tuple[str, BulkPaymentResponse | None]:
    """Return ("created" | "replayed", body), or (error code, None) for a domain error."""
    try:
        result = run_service(payload, key, lock_timeout_ms)
    except DomainError as error:
        return error.code, None
    return ("replayed" if result.replayed else "created"), result.body


def reset_data(db: Session) -> None:
    """Back to the seeded state, through db itself so its open transaction cannot block it."""
    db.execute(text("TRUNCATE payments, idempotency_keys, firms RESTART IDENTITY CASCADE"))
    db.connection().exec_driver_sql(SEED_SQL)
    db.commit()


def assert_ledger_consistent(db: Session, start: dict[str, int]) -> None:
    """I-1 and I-2, and every balance explained by the payments rows."""
    rows = db.execute(
        text(
            "SELECT f.uuid, f.balance_cents,"
            " (SELECT COALESCE(SUM(amount_cents), 0) FROM payments WHERE payer_firm_id = f.id),"
            " (SELECT COALESCE(SUM(amount_cents), 0) FROM payments WHERE payee_firm_id = f.id)"
            " FROM firms f"
        )
    ).all()
    current = {firm_uuid: balance for firm_uuid, balance, _, _ in rows}
    assert current.keys() == start.keys()
    negative = {firm_uuid: balance for firm_uuid, balance in current.items() if balance < 0}
    assert not negative, f"I-1 violated: {negative}"
    assert sum(current.values()) == sum(start.values()), "I-2 violated: the sum of balances moved"
    for firm_uuid, balance, paid, received in rows:
        assert balance == start[firm_uuid] - paid + received, f"ledger mismatch for {firm_uuid}"


def wait_until_blocked(db: Session, timeout_s: float = 5.0) -> None:
    """Return once another backend of this database waits for a lock; fail on timeout."""
    deadline = time.monotonic() + timeout_s
    while True:
        waiting = db.execute(
            text(
                "SELECT count(*) FROM pg_stat_activity"
                " WHERE datname = current_database() AND pid <> pg_backend_pid()"
                " AND wait_event_type = 'Lock'"
            )
        ).scalar_one()
        # pg_stat_activity is a snapshot per transaction: end it before the next poll.
        db.rollback()
        if waiting:
            return
        if time.monotonic() > deadline:
            pytest.fail(f"no backend waited for a lock within {timeout_s} s")
        time.sleep(0.01)
