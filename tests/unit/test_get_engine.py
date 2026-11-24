"""One engine per process: its pool is what bounds the process's database connections."""

import threading
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine

import app.db
from app.db import get_engine
from app.main import app as application

THREADS = 16


@pytest.fixture
def no_engine_yet(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[Engine]]:
    """Start without an engine; restore the process's engine and dispose the new ones after.

    Creating an engine opens no connection, so these tests need no database.
    """
    monkeypatch.setattr(app.db, "_engine", None)
    created: list[Engine] = []
    yield created
    for engine in created:
        engine.dispose()


def test_concurrent_first_calls_share_one_engine(no_engine_yet):
    barrier = threading.Barrier(THREADS)

    def first_call(_: int) -> Engine:
        barrier.wait()
        return get_engine()

    with ThreadPoolExecutor(max_workers=THREADS) as executor:
        engines = list(executor.map(first_call, range(THREADS)))
    no_engine_yet.extend({id(engine): engine for engine in engines}.values())

    assert len(no_engine_yet) == 1, f"{len(no_engine_yet)} engines from {THREADS} first calls"


def test_startup_builds_the_engine(no_engine_yet):
    with TestClient(application):
        engine = app.db._engine
    assert engine is not None
    no_engine_yet.append(engine)
    assert get_engine() is engine
