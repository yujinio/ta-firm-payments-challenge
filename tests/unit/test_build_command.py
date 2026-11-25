import re

import pytest

from app.errors import SelfPayment, ValidationFailed
from app.schemas import BulkPaymentRequest
from app.service import build_command
from tests.conftest import LOPEZ, NAIR, PINECREST, SAMPLE_REQUEST, bulk


def _request(payload):
    return BulkPaymentRequest.model_validate(payload)


def _error_locs(excinfo):
    return [detail["loc"] for detail in excinfo.value.details]


def test_sample_request_totals(settings):
    cmd = build_command(BulkPaymentRequest.model_validate(SAMPLE_REQUEST), None, settings)
    assert cmd.total_cents == 1_325_125
    assert cmd.credits == {NAIR: 1_205_050, LOPEZ: 120_075}
    assert cmd.firm_uuids == {PINECREST, NAIR, LOPEZ}
    assert [i.amount_cents for i in cmd.items] == [625_000, 580_050, 120_075]
    assert cmd.idempotency_key is None and cmd.request_hash is None


def test_uuids_are_lowercased(settings):
    payload = bulk(PINECREST.upper(), (NAIR.upper(), "1"), ("{" + LOPEZ.upper() + "}", "2"))
    cmd = build_command(_request(payload), None, settings)
    assert cmd.payer_uuid == PINECREST
    assert [i.payee_uuid for i in cmd.items] == [NAIR, LOPEZ]
    assert cmd.firm_uuids == {PINECREST, NAIR, LOPEZ}


def test_self_payment_rejected(settings):
    payload = bulk(PINECREST, (NAIR, "1"), (PINECREST.upper(), "2"))
    with pytest.raises(SelfPayment):
        build_command(_request(payload), None, settings)


def test_description_too_long(settings):
    payload = bulk(PINECREST, (NAIR, "1"), (LOPEZ, "2"))
    payload["payments"][0]["description"] = "x" * 500
    build_command(_request(payload), None, settings)
    payload["payments"][1]["description"] = "x" * 501
    with pytest.raises(ValidationFailed) as excinfo:
        build_command(_request(payload), None, settings)
    assert _error_locs(excinfo) == [["body", "payments", 1, "description"]]


def test_total_overflow(settings):
    build_command(_request(bulk(PINECREST, (NAIR, "21474836.46"), (LOPEZ, "0.01"))), None, settings)
    payload = bulk(PINECREST, (NAIR, "21474836.47"), (LOPEZ, "21474836.47"))
    with pytest.raises(ValidationFailed) as excinfo:
        build_command(_request(payload), None, settings)
    assert _error_locs(excinfo) == [["body", "payments"]]


@pytest.mark.parametrize("key", ["", "a" * 256, "has space", "ключ"])
def test_invalid_idempotency_key(settings, key):
    with pytest.raises(ValidationFailed) as excinfo:
        build_command(_request(SAMPLE_REQUEST), key, settings)
    assert _error_locs(excinfo) == [["header", "Idempotency-Key"]]


def test_valid_key_produces_hash(settings):
    request = BulkPaymentRequest.model_validate(SAMPLE_REQUEST)
    cmd = build_command(request, "a" * 255, settings)
    assert cmd.idempotency_key == "a" * 255
    assert re.fullmatch(r"[0-9a-f]{64}", cmd.request_hash)


def test_hash_ignores_uuid_and_amount_spelling(settings):
    plain = bulk(PINECREST, (NAIR, "300"))
    respelled = bulk(PINECREST.upper(), ("{" + NAIR.upper() + "}", "300.00"))
    first = build_command(_request(plain), "key-1", settings)
    second = build_command(_request(respelled), "key-1", settings)
    assert first.request_hash == second.request_hash
