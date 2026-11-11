"""The agent's copy of the canonical refund payload and its hash (ADR-004 step 8).

refund-api has its own implementation (ADR-007: no shared code); ``contracts/hash_vectors.json``
keeps the two identical. Inputs are hashed as given: no case folding, no Unicode normalization.
"""

import hashlib
import json


def canonical_payload(*, order_id: str, amount_minor: int, currency: str, action_id: str) -> str:
    for name, value in (("order_id", order_id), ("currency", currency), ("action_id", action_id)):
        if not isinstance(value, str):
            raise TypeError(f"{name} must be str, got {type(value).__name__}")
    if type(amount_minor) is not int:
        raise TypeError(f"amount_minor must be int, got {type(amount_minor).__name__}")
    return json.dumps(
        {
            "action_id": action_id,
            "amount_minor": amount_minor,
            "currency": currency,
            "order_id": order_id,
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )


def payload_hash(*, order_id: str, amount_minor: int, currency: str, action_id: str) -> str:
    canonical = canonical_payload(
        order_id=order_id, amount_minor=amount_minor, currency=currency, action_id=action_id
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
