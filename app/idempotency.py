import hashlib
import json
from collections.abc import Sequence


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
