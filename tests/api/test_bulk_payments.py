import copy
import json
import logging
import uuid

from sqlalchemy import text

from app.schemas import INT32_MAX, format_cents
from tests.conftest import (
    FORMAT_EXAMPLE_REQUEST,
    LOPEZ,
    NAIR,
    PINECREST,
    SAMPLE_REQUEST,
    balances,
    bulk,
    payment_rows,
    set_balance,
)


def test_a1_sample_request(client, db):
    r = client.post("/bulk-payments", json=SAMPLE_REQUEST)
    assert r.status_code == 201
    assert balances(db) == {PINECREST: 3_674_875, LOPEZ: 170_075, NAIR: 1_405_050}
    body = r.json()
    assert body["payer_firm_uuid"] == PINECREST
    assert body["total_amount"] == "13251.25"
    assert [p["amount"] for p in body["payments"]] == ["6250.00", "5800.50", "1200.75"]
    assert sorted(payment_rows(db)) == sorted(
        [
            (1, 3, 625_000, "Overflow returns, August 2026"),
            (1, 3, 580_050, "Amended returns, August 2026"),
            (1, 2, 120_075, "Bookkeeping cleanup, 3 clients"),
        ]
    )


def test_a1_response_ids_match_rows(client, db):
    body = client.post("/bulk-payments", json=SAMPLE_REQUEST).json()
    assert list(body) == ["payer_firm_uuid", "total_amount", "payments"]
    stored = {
        row_id: (payee, amount_cents, description)
        for row_id, payee, amount_cents, description in db.execute(
            text(
                "SELECT p.id, f.uuid, p.amount_cents, p.description"
                " FROM payments p JOIN firms f ON f.id = p.payee_firm_id"
            )
        ).all()
    }
    assert len(stored) == 3
    for returned in body["payments"]:
        assert list(returned) == ["id", "payee_firm_uuid", "amount", "description"]
        payee, amount_cents, description = stored[returned["id"]]
        assert returned["payee_firm_uuid"] == payee
        assert returned["amount"] == format_cents(amount_cents)
        assert returned["description"] == description


def assert_rejected(r, code, db, before):
    assert r.status_code == 422
    assert r.json()["error"]["code"] == code
    assert balances(db) == before and payment_rows(db) == []


def test_a2_insufficient_funds(client, db):
    before = balances(db)
    r = client.post("/bulk-payments", json=bulk(LOPEZ, (NAIR, "500.01")))
    assert_rejected(r, "insufficient_funds", db, before)


def test_a3_exact_balance(client, db):
    r = client.post("/bulk-payments", json=bulk(LOPEZ, (NAIR, "500")))
    assert r.status_code == 201
    after = balances(db)
    assert after[LOPEZ] == 0
    assert after[NAIR] == 250_000


def test_a4_one_cent_short(client, db):
    before = balances(db)
    r = client.post("/bulk-payments", json=bulk(LOPEZ, (NAIR, "300"), (PINECREST, "200.01")))
    assert_rejected(r, "insufficient_funds", db, before)


def test_a5_unknown_payee(client, db):
    before = balances(db)
    r = client.post("/bulk-payments", json=bulk(PINECREST, (NAIR, "1"), (str(uuid.uuid4()), "1")))
    assert_rejected(r, "firm_not_found", db, before)


def test_a6_unknown_payer(client, db):
    before = balances(db)
    r = client.post("/bulk-payments", json=bulk(str(uuid.uuid4()), (NAIR, "1")))
    assert_rejected(r, "firm_not_found", db, before)


def test_a7_self_payment(client, db):
    before = balances(db)
    r = client.post("/bulk-payments", json=bulk(PINECREST, (PINECREST, "1")))
    assert_rejected(r, "self_payment", db, before)


def test_a8_repeated_payee(client, db):
    r = client.post("/bulk-payments", json=bulk(PINECREST, (NAIR, "1"), (NAIR, "2"), (NAIR, "3")))
    assert r.status_code == 201
    assert balances(db)[NAIR] == 200_600
    assert payment_rows(db) == [(1, 3, 100, "p1"), (1, 3, 200, "p2"), (1, 3, 300, "p3")]


def test_a10_uppercase_uuid(client, db):
    payload = copy.deepcopy(SAMPLE_REQUEST)
    payload["payer_firm_uuid"] = payload["payer_firm_uuid"].upper()
    for payment in payload["payments"]:
        payment["payee_firm_uuid"] = payment["payee_firm_uuid"].upper()
    r = client.post("/bulk-payments", json=payload)
    assert r.status_code == 201
    body = r.json()
    assert body["payer_firm_uuid"] == PINECREST
    assert [p["payee_firm_uuid"] for p in body["payments"]] == [NAIR, NAIR, LOPEZ]
    assert balances(db) == {PINECREST: 3_674_875, LOPEZ: 170_075, NAIR: 1_405_050}


def test_a11_balance_overflow(client, db):
    set_balance(db, NAIR, INT32_MAX)
    before = balances(db)
    r = client.post("/bulk-payments", json=bulk(PINECREST, (NAIR, "0.01")))
    assert_rejected(r, "balance_overflow", db, before)


def test_a13_response_order(client):
    r = client.post("/bulk-payments", json=bulk(PINECREST, (LOPEZ, "3"), (NAIR, "1"), (LOPEZ, "2")))
    assert r.status_code == 201
    payments = r.json()["payments"]
    assert [p["amount"] for p in payments] == ["3.00", "1.00", "2.00"]
    assert [p["payee_firm_uuid"] for p in payments] == [LOPEZ, NAIR, LOPEZ]
    assert [p["description"] for p in payments] == ["p1", "p2", "p3"]
    ids = [p["id"] for p in payments]
    assert len(set(ids)) == 3


def _keys(value):
    if isinstance(value, dict):
        for key, item in value.items():
            yield key
            yield from _keys(item)
    elif isinstance(value, list):
        for item in value:
            yield from _keys(item)


def test_response_has_no_balance(client):
    r = client.post("/bulk-payments", json=SAMPLE_REQUEST)
    assert r.status_code == 201
    assert [key for key in _keys(r.json()) if "balance" in key] == []


def test_a16_format_example_request(client, db):
    r = client.post("/bulk-payments", json=FORMAT_EXAMPLE_REQUEST)
    assert r.status_code == 201
    assert r.json()["total_amount"] == "1500.75"
    assert balances(db) == {PINECREST: 4_849_925, LOPEZ: 170_075, NAIR: 230_000}
    assert len(payment_rows(db)) == 2


def _request_records(caplog):
    return [json.loads(rec.getMessage()) for rec in caplog.records if rec.name == "app.requests"]


def test_request_log_record(client, caplog):
    caplog.set_level(logging.INFO, logger="app")
    r = client.post("/bulk-payments", json=SAMPLE_REQUEST, headers={"X-Request-Id": "req-42"})
    assert r.status_code == 201
    [record] = _request_records(caplog)
    assert record["request_id"] == "req-42"
    assert record["instance"]
    assert record["payer"] == PINECREST
    assert record["payments"] == 3
    assert record["total"] == "13251.25"
    assert record["outcome"] == "created"
    assert record["status"] == 201
    assert isinstance(record["duration_ms"], int | float)
    for payment in SAMPLE_REQUEST["payments"]:
        assert payment["description"] not in caplog.text


def test_request_log_record_for_rejections(client, caplog):
    caplog.set_level(logging.INFO, logger="app")
    client.post("/bulk-payments", json=bulk(LOPEZ, (NAIR, "500.01")))
    client.post("/bulk-payments", json=bulk(LOPEZ, (NAIR, "0")))
    client.post(
        "/bulk-payments", content=b"{bad json", headers={"Content-Type": "application/json"}
    )
    insufficient, invalid_amount, bad_json = _request_records(caplog)
    assert insufficient["outcome"] == "insufficient_funds"
    assert (insufficient["payer"], insufficient["payments"]) == (LOPEZ, 1)
    assert insufficient["total"] == "500.01"
    assert invalid_amount["outcome"] == "validation_error"
    assert bad_json["outcome"] == "validation_error"
    assert bad_json["payer"] is None and bad_json["total"] is None
    request_ids = {insufficient["request_id"], invalid_amount["request_id"], bad_json["request_id"]}
    assert len(request_ids) == 3 and all(request_ids)
