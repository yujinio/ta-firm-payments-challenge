import pytest
from alembic import command
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from tests.conftest import LOPEZ, NAIR, PINECREST, alembic_config, balances


def test_seed_matches_task(db):
    assert balances(db) == {PINECREST: 5_000_000, LOPEZ: 50_000, NAIR: 200_000}


def test_firm_uuid_is_unique(db):
    with pytest.raises(IntegrityError):
        db.execute(
            text("INSERT INTO firms (id, name, balance_cents, uuid) VALUES (9, 'x', 0, :u)"),
            {"u": PINECREST},
        )


def test_payment_requires_existing_firms(db):
    with pytest.raises(IntegrityError):
        db.execute(
            text(
                "INSERT INTO payments (payer_firm_id, payee_firm_id, amount_cents, description)"
                " VALUES (1, 999, 1, 'x')"
            )
        )


def test_idempotency_key_is_unique_per_payer(db):
    insert = text(
        "INSERT INTO idempotency_keys (payer_firm_uuid, key, request_hash) VALUES (:p, 'k', 'h')"
    )
    db.execute(insert, {"p": PINECREST})
    db.execute(insert, {"p": LOPEZ})  # same key, other payer: allowed
    with pytest.raises(IntegrityError):
        db.execute(insert, {"p": PINECREST})


def test_migration_round_trip():
    config = alembic_config()
    command.downgrade(config, "base")
    command.upgrade(config, "head")  # leave the schema at head for the tests that follow
