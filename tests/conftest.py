import json
import os
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

# On the host the default follows DB_PORT, the port docker-compose.yml publishes `db` on.
os.environ["DATABASE_URL"] = os.environ.get(
    "TEST_DATABASE_URL",
    "postgresql+psycopg://postgres:postgres@localhost:"
    f"{os.environ.get('DB_PORT') or '5432'}/firm_payments_test",
)

import pytest  # noqa: E402
from alembic import command  # noqa: E402
from alembic.config import Config  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import text  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from app.db import get_engine  # noqa: E402
from app.main import app  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
UNIT_DIR = Path(__file__).resolve().parent / "unit"

PINECREST = "3f1c9a2e-7b4d-4c1e-9a55-2d8e6f0b7c41"
LOPEZ = "8b2e4c71-0d3a-4f6e-b1c9-5a7d2e9f4c10"
NAIR = "e5f18b3c-2a9d-4c07-8e6b-1d4a7f9c3b25"

SAMPLE_REQUEST = json.loads((ROOT / "sample-request.json").read_text())
FORMAT_EXAMPLE_REQUEST = json.loads((ROOT / "format-example-request.json").read_text())

SEED_SQL = (ROOT / "scripts" / "seed.sql").read_text()


def alembic_config() -> Config:
    """Alembic configuration of the repository; the database comes from DATABASE_URL."""
    return Config(str(ROOT / "alembic.ini"))


def _is_unit_test(request: pytest.FixtureRequest) -> bool:
    """Tell whether the requesting test lives under tests/unit, which needs no database."""
    return UNIT_DIR in Path(str(request.path)).parents


@pytest.fixture(scope="session")
def _schema() -> None:
    """Migrate the test database to head, once per session."""
    command.upgrade(alembic_config(), "head")


@pytest.fixture(autouse=True)
def _fresh_data(request: pytest.FixtureRequest) -> None:
    """Migrate once per session, then reset and seed the data before every non-unit test."""
    if _is_unit_test(request):
        return
    request.getfixturevalue("_schema")
    with get_engine().begin() as conn:
        conn.exec_driver_sql("TRUNCATE payments, idempotency_keys, firms RESTART IDENTITY CASCADE")
        conn.exec_driver_sql(SEED_SQL)


@pytest.fixture
def client() -> Iterator[TestClient]:
    """A client of the app with its lifespan running; server errors come back as 500s."""
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c


@pytest.fixture
def db() -> Iterator[Session]:
    """A session on the app's engine to arrange and check data; it commits only when told to."""
    with Session(get_engine(), expire_on_commit=False) as session:
        yield session


def balances(db: Session) -> dict[str, int]:
    """Every firm's balance in cents, by uuid."""
    rows = db.execute(text("SELECT uuid, balance_cents FROM firms")).all()
    return {u: cents for u, cents in rows}


def payment_rows(db: Session) -> list[tuple[int, int, int, str]]:
    """Every payment as (payer id, payee id, amount in cents, description), in id order."""
    rows = db.execute(
        text(
            "SELECT payer_firm_id, payee_firm_id, amount_cents, description"
            " FROM payments ORDER BY id"
        )
    ).all()
    return [(a, b, c, d) for a, b, c, d in rows]


def add_firm(db: Session, firm_id: int, balance_cents: int) -> str:
    """Insert and commit a firm with a fresh uuid, and return the uuid."""
    firm_uuid = str(uuid.uuid4())
    db.execute(
        text("INSERT INTO firms (id, name, balance_cents, uuid) VALUES (:i, :n, :b, :u)"),
        {"i": firm_id, "n": f"Firm {firm_id}", "b": balance_cents, "u": firm_uuid},
    )
    db.commit()
    return firm_uuid


def set_balance(db: Session, uuid: str, balance_cents: int) -> None:
    """Set a firm's balance and commit."""
    db.execute(
        text("UPDATE firms SET balance_cents = :b WHERE uuid = :u"),
        {"b": balance_cents, "u": uuid},
    )
    db.commit()


# Any: a JSON request body that tests also edit into invalid shapes before sending it.
def bulk(payer: str, *payments: tuple[str, str]) -> dict[str, Any]:
    """Build a request body from (payee uuid, amount) pairs; descriptions are p1, p2, ..."""
    return {
        "payer_firm_uuid": payer,
        "payments": [
            {"amount": amount, "payee_firm_uuid": payee, "description": f"p{i}"}
            for i, (payee, amount) in enumerate(payments, start=1)
        ],
    }
