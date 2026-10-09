"""Canonical refund payload and the hash a key is bound to (ADR-004 step 8).

The agent has its own copy of this (ADR-007: no shared code); ``contracts/hash_vectors.json``
keeps the two in step. Inputs are hashed exactly as given: no case folding, no Unicode
normalization. Validation rejects malformed values before they get here, and normalizing on
one side only would be a silent mismatch.
"""

import hashlib
import json


def canonical_payload(*, order_id: str, amount_minor: int, currency: str, action_id: str) -> str:
    """Sorted keys, no whitespace, raw UTF-8, integer minor units only."""
    for name, value in (("order_id", order_id), ("currency", currency), ("action_id", action_id)):
        if not isinstance(value, str):
            raise TypeError(f"{name} must be str, got {type(value).__name__}")
    # bool is a subclass of int, and json would encode it as true/false.
    if type(amount_minor) is not int:
        raise TypeError(f"amount_minor must be int, got {type(amount_minor).__name__}")
    fields = {
        "order_id": order_id,
        "amount_minor": amount_minor,
        "currency": currency,
        "action_id": action_id,
    }
    return json.dumps(fields, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def payload_hash(*, order_id: str, amount_minor: int, currency: str, action_id: str) -> str:
    """Lowercase hex SHA-256 of the canonical payload's UTF-8 bytes."""
    canonical = canonical_payload(
        order_id=order_id, amount_minor=amount_minor, currency=currency, action_id=action_id
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
