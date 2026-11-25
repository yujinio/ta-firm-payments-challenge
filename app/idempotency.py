import hashlib
import json
from collections.abc import Sequence
from typing import cast

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.errors import IdempotencyKeyReuse
from app.models import IdempotencyKey
from app.schemas import BulkPaymentResponse


def compute_request_hash(payer_uuid: str, items: Sequence[tuple[str, int, str]]) -> str:
    """SHA-256 of the parsed request; items are (payee_uuid, amount_cents, description).

    The canonical form must never change: stored hashes have to stay comparable.
    """
    canonical = json.dumps(
        {"payer": payer_uuid, "payments": [list(item) for item in items]},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def claim_key(
    session: Session, payer_uuid: str, key: str, request_hash: str
) -> BulkPaymentResponse | None:
    """Claim the key for this transaction, or return the response stored under it.

    Returns None when the key is new. If another transaction holds an uncommitted
    row for the key, the insert waits for its outcome (bounded by lock_timeout):
    after a commit the stored response is returned, after a rollback the key is
    claimed here.
    """
    claimed_id = session.execute(
        # response_body is left out: SQLAlchemy's JSON type would store None as JSON null.
        insert(IdempotencyKey)
        .values(payer_firm_uuid=payer_uuid, key=key, request_hash=request_hash)
        .on_conflict_do_nothing(index_elements=["payer_firm_uuid", "key"])
        .returning(IdempotencyKey.id)
    ).scalar_one_or_none()
    if claimed_id is not None:
        return None

    # A new statement: under Read Committed it sees the row that won the conflict.
    existing = session.execute(
        select(IdempotencyKey.request_hash, IdempotencyKey.response_body).where(
            IdempotencyKey.payer_firm_uuid == payer_uuid, IdempotencyKey.key == key
        )
    ).one()
    if existing.response_body is None:
        # The row and its response are committed together, so this cannot happen.
        raise RuntimeError("a committed idempotency key has no stored response")
    if existing.request_hash != request_hash:
        raise IdempotencyKeyReuse("the Idempotency-Key was already used with a different request")
    # Not checked at runtime: only store_response writes the column, with this shape.
    return cast(BulkPaymentResponse, existing.response_body)


def store_response(session: Session, payer_uuid: str, key: str, body: BulkPaymentResponse) -> None:
    """Store the 201 body under a key this transaction has claimed."""
    session.execute(
        update(IdempotencyKey)
        .where(IdempotencyKey.payer_firm_uuid == payer_uuid, IdempotencyKey.key == key)
        .values(response_body=body)
    )
