"""A01's review hash (ADR-009): binds an approval to exactly what the reviewer saw.

SHA-256 of the canonical JSON of the proposal, the sorted fired rule IDs and the policy version,
computed once at proposal time and stored on the run. Agent-only: it isn't ADR-004's payload
hash, so its vectors live in the agent's tests, not ``contracts/``. Inputs are hashed as given:
no case folding, no Unicode normalization.
"""

import hashlib
import json
from collections.abc import Iterable

from agent.core.policy import RefundProposal


def review_hash(proposal: RefundProposal, rule_ids: Iterable[str], policy_version: str) -> str:
    document = {
        "action_id": proposal.action_id,
        "amount_minor": proposal.amount_minor,
        "currency": proposal.currency,
        "order_id": proposal.order_id,
        "policy_version": policy_version,
        "rule_ids": sorted(rule_ids),
    }
    canonical = json.dumps(document, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
