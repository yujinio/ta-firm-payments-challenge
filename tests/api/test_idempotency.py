import json
import logging

import pytest
from sqlalchemy import text

from tests.conftest import (
    LOPEZ,
    NAIR,
    PINECREST,
    SAMPLE_REQUEST,
    balances,
    bulk,
    payment_rows,
    set_balance,
)

KEY = {"Idempotency-Key": "order-1"}


def key_rows(db):
    return db.execute(
        text("SELECT payer_firm_uuid, key, response_body FROM idempotency_keys ORDER BY id")
    ).all()


def test_i1_replay_returns_stored_response(client, db):
    first = client.post("/bulk-payments", json=SAMPLE_REQUEST, headers=KEY)
    second = client.post("/bulk-payments", json=SAMPLE_REQUEST, headers=KEY)
    assert first.status_code == second.status_code == 201
    assert second.content == first.content  # byte-identical replay
    assert "Idempotent-Replayed" not in first.headers
    assert second.headers["Idempotent-Replayed"] == "true"
    assert balances(db)[PINECREST] == 3_674_875
    assert len(payment_rows(db)) == 3


def test_i2_same_key_other_body(client, db):
    assert client.post("/bulk-payments", json=SAMPLE_REQUEST, headers=KEY).status_code == 201
    after_first = balances(db)
    r = client.post("/bulk-payments", json=bulk(PINECREST, (NAIR, "1")), headers=KEY)
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "idempotency_key_reuse"
    assert "Idempotent-Replayed" not in r.headers
    assert balances(db) == after_first
    assert len(payment_rows(db)) == 3


def test_i3_rejection_is_not_stored(client, db):
    payload = bulk(LOPEZ, (NAIR, "600"))
    first = client.post("/bulk-payments", json=payload, headers=KEY)
    assert first.status_code == 422
    assert first.json()["error"]["code"] == "insufficient_funds"
    set_balance(db, LOPEZ, 60_000)
    second = client.post("/bulk-payments", json=payload, headers=KEY)
    assert second.status_code == 201
    assert "Idempotent-Replayed" not in second.headers
    assert balances(db)[LOPEZ] == 0
    assert len(payment_rows(db)) == 1


def test_i4_key_is_scoped_to_payer(client, db):
    first = client.post("/bulk-payments", json=bulk(PINECREST, (LOPEZ, "1")), headers=KEY)
    second = client.post("/bulk-payments", json=bulk(NAIR, (LOPEZ, "1")), headers=KEY)
    assert first.status_code == second.status_code == 201
    assert "Idempotent-Replayed" not in first.headers
    assert "Idempotent-Replayed" not in second.headers
    assert second.json()["payer_firm_uuid"] == NAIR
    assert balances(db)[LOPEZ] == 50_200
    assert len(payment_rows(db)) == 2


def test_i6_no_key_executes_twice(client, db):
    first = client.post("/bulk-payments", json=SAMPLE_REQUEST)
    second = client.post("/bulk-payments", json=SAMPLE_REQUEST)
    assert first.status_code == second.status_code == 201
    assert "Idempotent-Replayed" not in second.headers
    assert balances(db)[PINECREST] == 2_349_750
    assert len(payment_rows(db)) == 6
    assert key_rows(db) == []


def test_i7_key_table_state(client, db):
    rejected = client.post(
        "/bulk-payments", json=bulk(LOPEZ, (NAIR, "600")), headers={"Idempotency-Key": "order-2"}
    )
    accepted = client.post("/bulk-payments", json=SAMPLE_REQUEST, headers=KEY)
    assert rejected.status_code == 422
    assert accepted.status_code == 201
    [(payer, key, response_body)] = key_rows(db)
    assert (payer, key) == (PINECREST, "order-1")
    assert response_body is not None
    assert response_body == accepted.json()


def test_i8_amount_spelling(client, db):
    first = client.post("/bulk-payments", json=bulk(PINECREST, (NAIR, "300")), headers=KEY)
    second = client.post("/bulk-payments", json=bulk(PINECREST, (NAIR, "300.00")), headers=KEY)
    assert first.status_code == second.status_code == 201
    assert second.headers["Idempotent-Replayed"] == "true"
    assert second.content == first.content
    assert balances(db)[NAIR] == 230_000
    assert len(payment_rows(db)) == 1


def test_committed_key_without_response_is_an_internal_error(client, db):
    # Cannot happen through the service: the row and its response commit together.
    db.execute(
        text(
            "INSERT INTO idempotency_keys (payer_firm_uuid, key, request_hash)"
            " VALUES (:payer, 'order-1', 'other-hash')"
        ),
        {"payer": PINECREST},
    )
    db.commit()
    before = balances(db)
    r = client.post("/bulk-payments", json=SAMPLE_REQUEST, headers=KEY)
    assert r.status_code == 500
    assert r.json()["error"]["code"] == "internal_error"
    assert balances(db) == before and payment_rows(db) == []


def test_replay_is_byte_identical_for_non_ascii_description(client):
    payload = bulk(PINECREST, (NAIR, "1"))
    payload["payments"][0]["description"] = "Überprüfung — 税務 🧾"
    first = client.post("/bulk-payments", json=payload, headers=KEY)
    second = client.post("/bulk-payments", json=payload, headers=KEY)
    assert first.status_code == second.status_code == 201
    assert second.headers["Idempotent-Replayed"] == "true"
    assert second.content == first.content


@pytest.mark.parametrize(
    "value",
    [b"", b"a" * 256, b"has space", "ключ".encode()],
    ids=["empty", "256_chars", "space", "non_ascii"],
)
def test_invalid_key_header(client, db, value):
    before = balances(db)
    r = client.post("/bulk-payments", json=SAMPLE_REQUEST, headers={"Idempotency-Key": value})
    assert r.status_code == 422
    error = r.json()["error"]
    assert error["code"] == "validation_error"
    assert [detail["loc"] for detail in error["details"]] == [["header", "Idempotency-Key"]]
    assert balances(db) == before and payment_rows(db) == [] and key_rows(db) == []


def test_key_header_sent_twice_uses_first(client, db):
    headers = [("Idempotency-Key", "a"), ("Idempotency-Key", "b")]
    first = client.post("/bulk-payments", json=SAMPLE_REQUEST, headers=headers)
    assert first.status_code == 201
    assert [key for _, key, _ in key_rows(db)] == ["a"]
    replay = client.post("/bulk-payments", json=SAMPLE_REQUEST, headers={"Idempotency-Key": "a"})
    assert replay.headers["Idempotent-Replayed"] == "true"
    assert balances(db)[PINECREST] == 3_674_875


def _request_records(caplog):
    return [json.loads(rec.getMessage()) for rec in caplog.records if rec.name == "app.requests"]


def test_request_log_outcomes(client, caplog):
    caplog.set_level(logging.INFO, logger="app")
    client.post("/bulk-payments", json=SAMPLE_REQUEST, headers=KEY)
    client.post("/bulk-payments", json=SAMPLE_REQUEST, headers=KEY)
    client.post("/bulk-payments", json=bulk(PINECREST, (NAIR, "1")), headers=KEY)
    created, replayed, reuse = _request_records(caplog)
    assert (created["outcome"], created["status"]) == ("created", 201)
    assert (replayed["outcome"], replayed["status"]) == ("replayed", 201)
    assert (reuse["outcome"], reuse["status"]) == ("idempotency_key_reuse", 422)
