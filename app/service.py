import logging
import re
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.exc import TimeoutError as PoolTimeoutError
from sqlalchemy.orm import Session

from app.config import Settings
from app.errors import (
    BalanceOverflow,
    FirmNotFound,
    InsufficientFunds,
    SelfPayment,
    ServiceBusy,
    ValidationFailed,
)
from app.idempotency import compute_request_hash
from app.models import Firm, Payment
from app.schemas import INT32_MAX, BulkPaymentRequest, format_cents, parse_amount_cents

logger = logging.getLogger(__name__)

_LOCK_NOT_AVAILABLE = "55P03"
_DEADLOCK_DETECTED = "40P01"

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


@dataclass(frozen=True)
class BulkPaymentResult:
    body: dict[str, Any]
    replayed: bool


def execute_bulk_payment(
    session: Session, command: BulkPaymentCommand, lock_timeout_ms: int
) -> BulkPaymentResult:
    """Apply the whole batch in one transaction, or reject it and change nothing.

    The session must not be in a transaction yet; this function owns it.
    """
    try:
        with session.begin():
            body = _apply(session, command, lock_timeout_ms)
    except DBAPIError as exc:
        sqlstate = getattr(exc.orig, "sqlstate", None)
        if sqlstate == _DEADLOCK_DETECTED:
            # Locks are taken in one query ordered by id, so this should not happen.
            # exc.orig, not exc: the SQLAlchemy message carries the statement
            # parameters, and those include payment descriptions.
            logger.error("deadlock detected in a bulk payment transaction: %s", exc.orig)
        if sqlstate in (_LOCK_NOT_AVAILABLE, _DEADLOCK_DETECTED):
            raise ServiceBusy("the payment could not be processed in time; retry") from exc
        raise
    except PoolTimeoutError as exc:
        raise ServiceBusy("no database connection is available; retry") from exc
    return BulkPaymentResult(body=body, replayed=False)


def _apply(session: Session, command: BulkPaymentCommand, lock_timeout_ms: int) -> dict[str, Any]:
    """Transaction steps of spec section 8.2; only database work happens here."""
    session.execute(
        text("SELECT set_config('lock_timeout', :value, true)"),
        {"value": f"{lock_timeout_ms}ms"},
    )

    # The only read of firms: it finds, checks and locks them in one consistent order.
    locked = session.scalars(
        select(Firm)
        .where(Firm.uuid.in_(command.firm_uuids))
        .order_by(Firm.id)
        .with_for_update(key_share=True)
        .execution_options(populate_existing=True)
    ).all()
    firms = {firm.uuid: firm for firm in locked}

    missing = command.firm_uuids - firms.keys()
    if missing:
        raise FirmNotFound(f"firms not found: {', '.join(sorted(missing))}")

    payer = firms[command.payer_uuid]
    if payer.balance_cents < command.total_cents:
        raise InsufficientFunds("the payer's balance does not cover the batch total")

    for payee_uuid, credit in command.credits.items():
        if firms[payee_uuid].balance_cents + credit > INT32_MAX:
            raise BalanceOverflow(f"the balance of payee {payee_uuid} would exceed the maximum")

    rows = [
        Payment(
            payer_firm_id=payer.id,
            payee_firm_id=firms[item.payee_uuid].id,
            amount_cents=item.amount_cents,
            description=item.description,
        )
        for item in command.items
    ]
    session.add_all(rows)
    session.flush()

    payer.balance_cents -= command.total_cents
    for payee_uuid, credit in command.credits.items():
        firms[payee_uuid].balance_cents += credit

    return {
        "payer_firm_uuid": command.payer_uuid,
        "total_amount": format_cents(command.total_cents),
        "payments": [
            {
                "id": row.id,
                "payee_firm_uuid": item.payee_uuid,
                "amount": format_cents(item.amount_cents),
                "description": item.description,
            }
            for row, item in zip(rows, command.items, strict=True)
        ],
    }
