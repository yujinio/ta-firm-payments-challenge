import hashlib
import re

import pytest

from app.idempotency import compute_request_hash
from app.schemas import parse_amount_cents

PAYER = "3f1c9a2e-7b4d-4c1e-9a55-2d8e6f0b7c41"
PAYEE = "e5f18b3c-2a9d-4c07-8e6b-1d4a7f9c3b25"
OTHER = "8b2e4c71-0d3a-4f6e-b1c9-5a7d2e9f4c10"

BASE_ITEMS = [(PAYEE, 100, "a"), (OTHER, 200, "b")]


def test_hash_ignores_amount_spelling():
    short = compute_request_hash(PAYER, [(PAYEE, parse_amount_cents("300"), "x")])
    padded = compute_request_hash(PAYER, [(PAYEE, parse_amount_cents("300.00"), "x")])
    assert short == padded


@pytest.mark.parametrize(
    "payer, items",
    [
        (OTHER, BASE_ITEMS),
        (PAYER, [(OTHER, 100, "a"), (OTHER, 200, "b")]),
        (PAYER, [(PAYEE, 101, "a"), (OTHER, 200, "b")]),
        (PAYER, [(PAYEE, 100, "c"), (OTHER, 200, "b")]),
        (PAYER, list(reversed(BASE_ITEMS))),
    ],
    ids=["payer", "payee", "amount", "description", "order"],
)
def test_hash_is_sensitive(payer, items):
    assert compute_request_hash(payer, items) != compute_request_hash(PAYER, BASE_ITEMS)


def test_hash_is_sha256_hex():
    assert re.fullmatch(r"[0-9a-f]{64}", compute_request_hash(PAYER, [(PAYEE, 100, "x")]))


def test_hash_canonical_form_is_fixed():
    canonical = f'{{"payer":"{PAYER}","payments":[["{PAYEE}",100,"café"]]}}'
    expected = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    assert compute_request_hash(PAYER, [(PAYEE, 100, "café")]) == expected
