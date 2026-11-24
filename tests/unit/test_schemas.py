import copy
import json
from typing import Any

import pytest
from pydantic import ValidationError

from app.schemas import BulkPaymentRequest
from tests.conftest import SAMPLE_REQUEST


def _request(**fields: Any) -> dict[str, Any]:
    payload = copy.deepcopy(SAMPLE_REQUEST)
    payload.update(fields)
    return payload


def _with_payment(**fields: Any) -> dict[str, Any]:
    payload = copy.deepcopy(SAMPLE_REQUEST)
    payload["payments"][0].update(fields)
    return payload


def _without_description() -> dict[str, Any]:
    payload = copy.deepcopy(SAMPLE_REQUEST)
    del payload["payments"][0]["description"]
    return payload


INVALID_REQUESTS = {
    "empty-payments": _request(payments=[]),
    "unknown-top-level-field": _request(currency="USD"),
    "unknown-payment-field": _with_payment(currency="USD"),
    # Decoded from JSON text, as the endpoint receives it; no float literal in the source.
    "amount-json-number": _with_payment(amount=json.loads("12.5")),
    "amount-null": _with_payment(amount=None),
    "amount-three-decimals": _with_payment(amount="1.234"),
    "description-number": _with_payment(description=5),
    "description-missing": _without_description(),
    "description-nul": _with_payment(description="a\u0000b"),
    "description-lone-surrogate": _with_payment(description="a\ud800b"),
    "payee-not-a-uuid": _with_payment(payee_firm_uuid="not-a-uuid"),
}


@pytest.mark.parametrize("payload", INVALID_REQUESTS.values(), ids=INVALID_REQUESTS.keys())
def test_invalid_request(payload):
    with pytest.raises(ValidationError):
        BulkPaymentRequest.model_validate(payload)


# The escape \ud800 written into the raw body, as a client would send it.
LONE_SURROGATE_BODY = json.dumps(SAMPLE_REQUEST).replace("Overflow returns", "Overflow \\ud800")


def test_lone_surrogate_decoded_by_json_loads_is_rejected():
    # FastAPI decodes the body with json.loads, which keeps the lone surrogate.
    payload = json.loads(LONE_SURROGATE_BODY)
    assert "\ud800" in payload["payments"][0]["description"]
    with pytest.raises(ValidationError):
        BulkPaymentRequest.model_validate(payload)


def test_lone_surrogate_in_raw_json_is_rejected():
    with pytest.raises(ValidationError):
        BulkPaymentRequest.model_validate_json(LONE_SURROGATE_BODY)


def test_sample_request_is_valid():
    request = BulkPaymentRequest.model_validate(SAMPLE_REQUEST)
    assert [p.amount for p in request.payments] == ["6250", "5800.5", "1200.75"]
