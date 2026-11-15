"""A01's review hash (ADR-009): agent-only vectors, computed from hand-written canonical strings.

Each expected hash is the SHA-256 of the canonical JSON written out in the comment, so a change
to the field set, key order, separators or rule-ID sorting fails here.
"""

import pytest

from agent.core.policy import RefundProposal
from agent.core.review_hash import review_hash

VECTORS = [
    # {"action_id":"act_1","amount_minor":2500,"currency":"USD","order_id":"ord_demo_1001",
    #  "policy_version":"1.0.0","rule_ids":[]}
    (
        RefundProposal("act_1", "ord_demo_1001", 2500, "USD"),
        (),
        "1c06c60f1096855afc7568fe96f09be36f556d988b81047185266ca4413b65ab",
    ),
    # ..."rule_ids":["R07_AUTO_APPROVE_MAX","R12_OTHER_ACTION_ON_ORDER"]} (given unsorted)
    (
        RefundProposal("act_2", "ord_demo_1002", 7000, "USD"),
        ("R12_OTHER_ACTION_ON_ORDER", "R07_AUTO_APPROVE_MAX"),
        "f1d3e4fe095f5168156bff400e6559789bf075cf706e60fb62c3524f6f9e6133",
    ),
    # Precomposed and decomposed "e acute" hash differently: no normalization.
    (
        RefundProposal("act_3", "ord_café", 100, "USD"),
        (),
        "0add0f7fc96e30ff5b81b4ac17a25910fdc806ac5e3cbb152a5c8ff91154578b",
    ),
    (
        RefundProposal("act_3", "ord_café", 100, "USD"),
        (),
        "f90849c47c6a3bb8ef8568f091bd2a95c486934b8e708bf038e570e4f236339b",
    ),
]


@pytest.mark.parametrize(("proposal", "rule_ids", "expected"), VECTORS)
def test_review_hash_matches_its_vector(
    proposal: RefundProposal, rule_ids: tuple[str, ...], expected: str
) -> None:
    assert review_hash(proposal, rule_ids, "1.0.0") == expected


def test_every_bound_field_changes_the_hash() -> None:
    base = RefundProposal("act_1", "ord_demo_1001", 2500, "USD")
    variants = [
        review_hash(RefundProposal("act_x", "ord_demo_1001", 2500, "USD"), (), "1.0.0"),
        review_hash(RefundProposal("act_1", "ord_other", 2500, "USD"), (), "1.0.0"),
        review_hash(RefundProposal("act_1", "ord_demo_1001", 2501, "USD"), (), "1.0.0"),
        review_hash(RefundProposal("act_1", "ord_demo_1001", 2500, "EUR"), (), "1.0.0"),
        review_hash(base, ("R07_AUTO_APPROVE_MAX",), "1.0.0"),
        review_hash(base, (), "1.0.1"),
    ]
    assert len({review_hash(base, (), "1.0.0"), *variants}) == len(variants) + 1
