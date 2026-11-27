"""What code checks in the warrant model's answer (ADR-029 W01), before policy sees it.

The model says whether the customer described a problem; code decides whether to believe it: the
answer must name the refund's order, a problem other than ``none``, and quote words the
customer actually wrote (after case and whitespace normalisation, at least ``MIN_QUOTE_CHARS``
of one customer message). A quote found only in the assistant's replies fails here.
"""

from collections.abc import Sequence

from agent.core.c01 import normalize
from agent.core.policy import WarrantStatus

MIN_QUOTE_CHARS = 8
NO_PROBLEM = "none"


def quote_is_customers(quote: str, customer_messages: Sequence[str]) -> bool:
    wanted = normalize(quote)
    return len(wanted) >= MIN_QUOTE_CHARS and any(wanted in normalize(m) for m in customer_messages)


def judge(
    *,
    warranted: bool,
    order_id: str,
    problem: str,
    quote: str,
    proposal_order_id: str,
    customer_messages: Sequence[str],
) -> WarrantStatus:
    if not warranted or problem == NO_PROBLEM:
        return WarrantStatus.NOT_WARRANTED
    if order_id != proposal_order_id:
        return WarrantStatus.OTHER_ORDER
    if not quote_is_customers(quote, customer_messages):
        return WarrantStatus.UNPROVEN_QUOTE
    return WarrantStatus.WARRANTED
