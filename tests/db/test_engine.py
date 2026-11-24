import logging

import pytest
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from app.db import get_engine
from app.service import BulkPaymentCommand, PaymentItem, execute_bulk_payment
from tests.conftest import NAIR, PINECREST, balances, payment_rows


def test_database_errors_do_not_carry_descriptions(
    db: Session, caplog: pytest.LogCaptureFixture
) -> None:
    """An unexpected DBAPIError is logged with its text by the server; spec 12 keeps
    descriptions out of logs, so the error text must not carry statement parameters."""
    caplog.set_level(logging.DEBUG)
    before = balances(db)
    # Built by hand to bypass validation: U+0000 makes the payments INSERT fail.
    command = BulkPaymentCommand(
        payer_uuid=PINECREST,
        items=(
            PaymentItem(NAIR, 100, "first-secret-description"),
            PaymentItem(NAIR, 100, "second-secret-description\x00"),
        ),
        total_cents=200,
        credits={NAIR: 200},
        firm_uuids=frozenset({PINECREST, NAIR}),
        idempotency_key=None,
        request_hash=None,
    )
    with Session(get_engine()) as session, pytest.raises(DBAPIError) as excinfo:
        execute_bulk_payment(session, command, 5000)
    assert "INSERT INTO payments" in str(excinfo.value)
    for text in (str(excinfo.value), repr(excinfo.value), caplog.text):
        assert "secret-description" not in text
    assert balances(db) == before and payment_rows(db) == []
