import json

import pytest

from app import api
from app.db import get_session
from app.errors import ServiceBusy
from app.main import app
from app.schemas import INT32_MAX
from tests.conftest import LOPEZ, NAIR, PINECREST, balances, bulk, payment_rows, set_balance

JSON = {"Content-Type": "application/json"}


def _with_description(description):
    payload = bulk(PINECREST, (NAIR, "1"))
    payload["payments"][0]["description"] = description
    return payload


def _with_amount(amount):
    payload = bulk(PINECREST, (NAIR, "1"))
    payload["payments"][0]["amount"] = amount
    return payload


SCHEMA_VIOLATIONS = {
    "empty_payments": bulk(PINECREST),
    "extra_field": {**bulk(PINECREST, (NAIR, "1")), "note": "x"},
    "amount_as_number": _with_amount(100),
    "amount_zero": _with_amount("0"),
    "description_501": _with_description("x" * 501),
    "payments_1001": bulk(PINECREST, *[(NAIR, "0.01")] * 1001),
    "description_nul": _with_description("a\x00b"),
}


def assert_validation_error(r):
    assert r.status_code == 422
    error = r.json()["error"]
    assert error["code"] == "validation_error"
    assert isinstance(error["details"], list) and error["details"]
    for detail in error["details"]:
        assert set(detail) == {"loc", "msg", "type"}
    return error


@pytest.mark.parametrize("payload", SCHEMA_VIOLATIONS.values(), ids=SCHEMA_VIOLATIONS.keys())
def test_a9_schema_violations(client, db, payload):
    before = balances(db)
    assert_validation_error(client.post("/bulk-payments", json=payload))
    assert balances(db) == before and payment_rows(db) == []


NON_OBJECT_BODIES = {
    "array": (b"[]", JSON),
    "string": (b'"x"', JSON),
    "null": (b"null", JSON),
    "number": (b"123", JSON),
    "bad_json": (b"{bad json", JSON),
    "empty": (b"", JSON),
    "text_plain": (
        json.dumps(bulk(PINECREST, (NAIR, "1"))).encode(),
        {"Content-Type": "text/plain"},
    ),
    "invalid_utf8": (b'{"payer_firm_uuid": "\xff"}', JSON),
}


@pytest.mark.parametrize(
    ("content", "headers"), NON_OBJECT_BODIES.values(), ids=NON_OBJECT_BODIES.keys()
)
def test_non_object_bodies(client, db, content, headers):
    r = client.post("/bulk-payments", content=content, headers=headers)
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "validation_error"
    assert payment_rows(db) == []


def test_lone_surrogate_in_description(client, db):
    # json.dumps escapes the lone surrogate, so the bytes on the wire carry the
    # six characters \ud800 and the server decodes them with json.loads.
    content = json.dumps(_with_description("\ud800")).encode("ascii")
    assert b"\\ud800" in content
    r = client.post("/bulk-payments", content=content, headers=JSON)
    error = assert_validation_error(r)
    assert error["details"][0]["loc"] == ["body", "payments", 0, "description"]
    assert payment_rows(db) == []


def test_details_do_not_echo_input(client):
    r = client.post("/bulk-payments", json=_with_amount("1x-secret-amount"))
    assert_validation_error(r)
    assert "1x-secret-amount" not in r.text


def test_a12_error_format(client, db):
    set_balance(db, NAIR, INT32_MAX)
    requests = {
        "validation_error": bulk(PINECREST),
        "self_payment": bulk(PINECREST, (PINECREST, "1")),
        "firm_not_found": bulk(PINECREST, (NAIR, "1"), ("0" * 32, "1")),
        "insufficient_funds": bulk(LOPEZ, (PINECREST, "500.01")),
        "balance_overflow": bulk(PINECREST, (NAIR, "0.01")),
    }
    for code, payload in requests.items():
        r = client.post("/bulk-payments", json=payload)
        assert r.status_code == 422
        body = r.json()
        assert set(body) == {"error"}
        assert body["error"]["code"] == code
        assert isinstance(body["error"]["message"], str) and body["error"]["message"]


@pytest.mark.parametrize(
    ("method", "path", "status", "code"),
    [
        ("GET", "/no-such-path", 404, "not_found"),
        ("GET", "/bulk-payments", 405, "method_not_allowed"),
    ],
)
def test_a12_http_error_format(client, method, path, status, code):
    r = client.request(method, path)
    assert r.status_code == status
    body = r.json()
    assert set(body) == {"error"}
    assert body["error"]["code"] == code
    assert isinstance(body["error"]["message"], str) and body["error"]["message"]


def test_a12_busy_error_format(client, monkeypatch):
    def busy(*args, **kwargs):
        raise ServiceBusy("the payment could not be processed in time; retry")

    monkeypatch.setattr(api, "execute_bulk_payment", busy)
    r = client.post("/bulk-payments", json=bulk(PINECREST, (NAIR, "1")))
    assert r.status_code == 503
    assert r.headers["Retry-After"] == "1"
    assert set(r.json()) == {"error"}
    assert r.json()["error"]["code"] == "busy"


@pytest.fixture
def failing_session():
    def boom():
        raise RuntimeError("boom")

    app.dependency_overrides[get_session] = boom
    try:
        yield
    finally:
        app.dependency_overrides.pop(get_session, None)


def test_a12_internal_error_format(client, failing_session):
    r = client.post("/bulk-payments", json=bulk(PINECREST, (NAIR, "1")))
    assert r.status_code == 500
    assert set(r.json()) == {"error"}
    assert r.json()["error"]["code"] == "internal_error"
    assert "boom" not in r.text
