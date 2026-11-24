"""C7: C1, C2 and I5 over HTTP against the service running as several processes.

The worker processes share no memory; they meet only in the database, as the replicas do.
"""

import os
import signal
import socket
import subprocess
import sys
import time
from collections import Counter
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from pathlib import Path

import httpx2
import pytest

from app.config import get_settings
from tests.concurrency.conftest import (
    SERVICE_ENGINE,
    assert_ledger_consistent,
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

WORKERS = 4
STARTUP_TIMEOUT_S = 10.0
STOP_TIMEOUT_S = 10.0
CLIENT_TIMEOUT_S = 30.0
CROSSING_ROUNDS = 10


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _stop(process: subprocess.Popen[bytes]) -> None:
    """SIGTERM lets Uvicorn stop its workers; SIGKILL to the whole group if it hangs."""
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=STOP_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGKILL)
        process.wait()


def _wait_until_ready(process: subprocess.Popen[bytes], base_url: str, log_path: Path) -> None:
    """Return once /healthz answers 200 and every worker has finished its startup."""
    deadline = time.monotonic() + STARTUP_TIMEOUT_S
    while time.monotonic() < deadline:
        if process.poll() is not None:
            pytest.fail(f"the server exited with {process.returncode}:\n{log_path.read_text()}")
        try:
            response = httpx2.get(f"{base_url}/healthz", timeout=1.0, trust_env=False)
            healthy = response.status_code == 200
        except httpx2.TransportError:
            healthy = False
        started = log_path.read_text().count("Application startup complete.")
        if healthy and started == WORKERS:
            return
        time.sleep(0.1)
    pytest.fail(f"the server was not ready within {STARTUP_TIMEOUT_S} s:\n{log_path.read_text()}")


@pytest.fixture(scope="module")
def live_server(_schema: None, tmp_path_factory: pytest.TempPathFactory) -> Iterator[str]:
    """Base URL of `uvicorn --workers 4` serving the test database.

    Connection budget: the in-process test engine (up to 50 connections) is disposed at the
    end of every module, and again here, so it holds none while these 4 x DB_POOL_SIZE
    connections may be open; the server is stopped before the next module starts.
    """
    SERVICE_ENGINE.dispose()
    port = _free_port()
    base_url = f"http://127.0.0.1:{port}"
    log_path = tmp_path_factory.mktemp("live_server") / "server.log"
    env = {**os.environ, "DATABASE_URL": get_settings().database_url}
    command = [sys.executable, "-m", "uvicorn", "app.main:app"]
    command += ["--host", "127.0.0.1", "--port", str(port), "--workers", str(WORKERS)]
    # A file, not a pipe: one log record per request would fill a pipe nobody reads.
    with log_path.open("wb") as log:
        process = subprocess.Popen(
            command, env=env, stdout=log, stderr=subprocess.STDOUT, start_new_session=True
        )
        try:
            _wait_until_ready(process, base_url, log_path)
            yield base_url
        finally:
            _stop(process)


@contextmanager
def http_clients(base_url: str, n: int) -> Iterator[list[httpx2.Client]]:
    """One client per thread, created before the threads start so they post together."""
    with ExitStack() as stack:
        yield [
            stack.enter_context(
                httpx2.Client(base_url=base_url, timeout=CLIENT_TIMEOUT_S, trust_env=False)
            )
            for _ in range(n)
        ]


def _outcome(response: httpx2.Response) -> tuple[int, str | None]:
    """The status and, for an error, its code."""
    if response.status_code == 201:
        return 201, None
    return response.status_code, response.json()["error"]["code"]


def test_c7_one_payer_over_http(live_server, db):
    payer = add_firm(db, 10, 100_000)
    start = balances(db)
    payload = bulk(payer, (LOPEZ, "100"), (NAIR, "200"))

    with http_clients(live_server, 10) as clients:
        responses = run_concurrently(
            lambda i: clients[i].post("/bulk-payments", json=payload), len(clients)
        )

    outcomes = Counter(_outcome(r) for r in responses)
    assert outcomes == {(201, None): 3, (422, "insufficient_funds"): 7}
    assert balances(db)[payer] == 10_000
    assert len(payment_rows(db)) == 6
    assert_ledger_consistent(db, start)


def test_c7_same_key_over_http(live_server, db):
    start = balances(db)
    headers = {"Idempotency-Key": "c7"}

    with http_clients(live_server, 8) as clients:
        responses = run_concurrently(
            lambda i: clients[i].post("/bulk-payments", json=SAMPLE_REQUEST, headers=headers),
            len(clients),
        )

    assert [r.status_code for r in responses] == [201] * 8
    replayed = Counter(r.headers.get("Idempotent-Replayed") for r in responses)
    assert replayed == {None: 1, "true": 7}
    assert all(r.content == responses[0].content for r in responses)
    assert len(payment_rows(db)) == 3
    assert balances(db)[PINECREST] == 3_674_875
    assert_ledger_consistent(db, start)


def test_c7_crossing_over_http(live_server, db):
    a = add_firm(db, 10, 10_000_000)
    b = add_firm(db, 11, 10_000_000)
    start = balances(db)
    # Interleaved, so both directions start at the barrier side by side.
    payloads = [bulk(a, (b, "1")), bulk(b, (a, "1"))] * 25

    with http_clients(live_server, len(payloads)) as clients:
        for _ in range(CROSSING_ROUNDS):
            responses = run_concurrently(
                lambda i: clients[i].post("/bulk-payments", json=payloads[i]), len(clients)
            )

            assert Counter(_outcome(r) for r in responses) == {(201, None): 50}
            current = balances(db)
            assert (current[a], current[b]) == (10_000_000, 10_000_000)
            assert_ledger_consistent(db, start)
