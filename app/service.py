import re
from dataclasses import dataclass
from typing import Any

from app.config import Settings
from app.errors import SelfPayment, ValidationFailed
from app.idempotency import compute_request_hash
from app.schemas import INT32_MAX, BulkPaymentRequest, format_cents, parse_amount_cents

# Visible ASCII, no spaces.
_IDEMPOTENCY_KEY_PATTERN = re.compile(r"[\x21-\x7e]{1,255}")


@dataclass(frozen=True)
class PaymentItem:
    payee_uuid: str
    amount_cents: int
    description: str


@dataclass(frozen=True)
class BulkPaymentCommand:
    payer_uuid: str
    items: tuple[PaymentItem, ...]
    total_cents: int
    credits: dict[str, int]
    firm_uuids: frozenset[str]
    idempotency_key: str | None
    request_hash: str | None


def _detail(loc: list[str | int], msg: str, type_: str) -> dict[str, Any]:
    return {"loc": loc, "msg": msg, "type": type_}


def build_command(
    request: BulkPaymentRequest, idempotency_key: str | None, settings: Settings
) -> BulkPaymentCommand:
    """Check the rules the schema cannot and derive what the transaction needs.

    Pure: no database access.
    """
    max_payments = settings.max_payments_per_request
    if len(request.payments) > max_payments:
        msg = f"at most {max_payments} payments are allowed per request"
        raise ValidationFailed(msg, [_detail(["body", "payments"], msg, "too_long")])

    max_length = settings.max_description_length
    msg = f"description must be at most {max_length} characters long"
    too_long = [
        _detail(["body", "payments", index, "description"], msg, "string_too_long")
        for index, payment in enumerate(request.payments)
        if len(payment.description) > max_length
    ]
    if too_long:
        raise ValidationFailed(msg, too_long)

    if idempotency_key is not None and _IDEMPOTENCY_KEY_PATTERN.fullmatch(idempotency_key) is None:
        msg = "Idempotency-Key must be 1 to 255 visible ASCII characters without spaces"
        detail = _detail(["header", "Idempotency-Key"], msg, "string_pattern_mismatch")
        raise ValidationFailed(msg, [detail])

    payer_uuid = str(request.payer_firm_uuid)
    items = tuple(
        PaymentItem(
            payee_uuid=str(payment.payee_firm_uuid),
            amount_cents=parse_amount_cents(payment.amount),
            description=payment.description,
        )
        for payment in request.payments
    )

    for index, item in enumerate(items):
        if item.payee_uuid == payer_uuid:
            raise SelfPayment(f"payment {index} pays the payer itself")

    total_cents = sum(item.amount_cents for item in items)
    if total_cents > INT32_MAX:
        msg = f"the batch total must be at most {format_cents(INT32_MAX)}"
        raise ValidationFailed(msg, [_detail(["body", "payments"], msg, "less_than_equal")])

    credits: dict[str, int] = {}
    for item in items:
        credits[item.payee_uuid] = credits.get(item.payee_uuid, 0) + item.amount_cents

    firm_uuids = frozenset({payer_uuid, *credits})

    request_hash = None
    if idempotency_key is not None:
        request_hash = compute_request_hash(
            payer_uuid, [(item.payee_uuid, item.amount_cents, item.description) for item in items]
        )

    return BulkPaymentCommand(
        payer_uuid=payer_uuid,
        items=items,
        total_cents=total_cents,
        credits=credits,
        firm_uuids=firm_uuids,
        idempotency_key=idempotency_key,
        request_hash=request_hash,
    )
