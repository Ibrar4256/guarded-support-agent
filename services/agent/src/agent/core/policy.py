"""Refund policy (ADR-009): pure rules with stable IDs, evaluated in two phases.

Every input, including the database clock, counts computed by the database, and the
configuration, is a parameter. Nothing here performs I/O.
"""

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import IntEnum, StrEnum

from agent.core.capacity import (
    RefundRecord,
    has_other_action_on_order,
    policy_approved_in_window,
    used_by_customer,
    used_on_order,
)

POLICY_VERSION = "1.0.0"


class Outcome(IntEnum):
    """Proposal-time outcomes, ordered by severity (higher wins)."""

    AUTO = 0
    REVIEW = 1
    OUT_OF_POLICY = 2
    FAIL_CLOSED = 3


class PresendOutcome(IntEnum):
    """Pre-send outcomes, ordered by severity (higher wins)."""

    PROCEED = 0
    HOLD = 1
    OUT_OF_POLICY = 2
    FAIL_CLOSED = 3


class RuleId(StrEnum):
    R01_AMOUNT_POSITIVE = "R01_AMOUNT_POSITIVE"
    R02_ORDER_PROVENANCE = "R02_ORDER_PROVENANCE"
    R03_ORDER_OWNERSHIP = "R03_ORDER_OWNERSHIP"
    R04_CURRENCY_MATCH = "R04_CURRENCY_MATCH"
    R05_ORDER_CHARGED_CAP = "R05_ORDER_CHARGED_CAP"
    R06_CUSTOMER_LIMIT = "R06_CUSTOMER_LIMIT"
    R07_AUTO_APPROVE_MAX = "R07_AUTO_APPROVE_MAX"
    R08_REFUND_WINDOW = "R08_REFUND_WINDOW"
    R09_ORDER_STATUS = "R09_ORDER_STATUS"
    R10_NO_CHARGEBACK = "R10_NO_CHARGEBACK"
    R11_AUTO_APPROVE_BUDGET = "R11_AUTO_APPROVE_BUDGET"
    R12_OTHER_ACTION_ON_ORDER = "R12_OTHER_ACTION_ON_ORDER"
    R13_CIRCUIT_BREAKER = "R13_CIRCUIT_BREAKER"
    R14_SENDS_HELD = "R14_SENDS_HELD"
    R15_SUPPORTED_CURRENCY = "R15_SUPPORTED_CURRENCY"
    R16_PENDING_REVIEW_CAP = "R16_PENDING_REVIEW_CAP"
    R17_DAILY_PROPOSAL_CAP = "R17_DAILY_PROPOSAL_CAP"
    R18_STICKY_ESCALATION = "R18_STICKY_ESCALATION"
    R19_GLOBAL_REVIEW_QUEUE_CAP = "R19_GLOBAL_REVIEW_QUEUE_CAP"


class FulfillmentStatus(StrEnum):
    """``orders.status`` is fulfillment only; refund state is derived (ADR-009)."""

    PENDING = "pending"
    SHIPPED = "shipped"
    DELIVERED = "delivered"
    CANCELLED = "cancelled"


class MissingInputError(ValueError):
    """A required input is absent; the rule fails closed instead of passing."""


class ConfigError(ValueError):
    """Invalid policy configuration; the service must refuse to start."""


@dataclass(frozen=True, slots=True)
class PolicyConfig:
    """Thresholds from settings (ADR-009 Config). Validate at startup."""

    currency: str
    auto_approve_max_minor: int
    auto_approve_budget_minor: int
    per_customer_limit_minor: int
    per_customer_window: timedelta
    refund_window: timedelta
    breaker_threshold_minor: int
    pending_review_cap: int
    daily_proposal_cap: int
    global_review_queue_cap: int
    decline_cooldown: timedelta

    def validate(self) -> None:
        problems: list[str] = []
        if len(self.currency) != 3 or not self.currency.isalpha() or not self.currency.isupper():
            problems.append("currency must be a 3-letter uppercase ISO code")
        positive_amounts = {
            "auto_approve_max_minor": self.auto_approve_max_minor,
            "auto_approve_budget_minor": self.auto_approve_budget_minor,
            "per_customer_limit_minor": self.per_customer_limit_minor,
            "breaker_threshold_minor": self.breaker_threshold_minor,
        }
        problems.extend(f"{name} must be > 0" for name, v in positive_amounts.items() if v <= 0)
        positive_counts = {
            "pending_review_cap": self.pending_review_cap,
            "daily_proposal_cap": self.daily_proposal_cap,
            "global_review_queue_cap": self.global_review_queue_cap,
        }
        problems.extend(f"{name} must be > 0" for name, v in positive_counts.items() if v <= 0)
        windows = {
            "per_customer_window": self.per_customer_window,
            "refund_window": self.refund_window,
            "decline_cooldown": self.decline_cooldown,
        }
        problems.extend(f"{name} must be > 0" for name, v in windows.items() if v <= timedelta(0))
        if self.auto_approve_budget_minor < self.auto_approve_max_minor:
            problems.append("auto_approve_budget_minor must be >= auto_approve_max_minor")
        if self.per_customer_limit_minor < self.auto_approve_budget_minor:
            problems.append("per_customer_limit_minor must be >= auto_approve_budget_minor")
        if problems:
            raise ConfigError("; ".join(problems))


@dataclass(frozen=True, slots=True)
class RefundProposal:
    action_id: str
    order_id: str
    amount_minor: int
    currency: str


@dataclass(frozen=True, slots=True)
class OrderFacts:
    """Order data loaded by code; never taken from model output."""

    order_id: str
    owner_user_id: str
    charged_minor: int
    currency: str
    status: FulfillmentStatus
    delivered_at: datetime | None
    chargeback_open: bool


@dataclass(frozen=True, slots=True)
class ReviewDecline:
    order_id: str
    declined_at: datetime


@dataclass(frozen=True, slots=True)
class PolicyControls:
    breaker_tripped: bool
    sends_enabled: bool


@dataclass(frozen=True, slots=True)
class ProposalInputs:
    """Everything proposal-time rules read. Counts are computed by the database."""

    proposal: RefundProposal
    session_user_id: str
    order: OrderFacts
    looked_up_order_ids: frozenset[str]
    history: Sequence[RefundRecord]
    declines: Sequence[ReviewDecline]
    has_unresolved_escalation: bool
    customer_pending_reviews: int
    customer_proposals_last_24h: int
    global_policy_approved_in_breaker_window_minor: int
    global_pending_reviews: int
    controls: PolicyControls
    now: datetime


@dataclass(frozen=True, slots=True)
class PresendInputs:
    """Everything pre-send rules read (ADR-004 step 6)."""

    proposal: RefundProposal
    session_user_id: str
    order: OrderFacts
    looked_up_order_ids: frozenset[str]
    history: Sequence[RefundRecord]
    approved_by_policy: bool
    controls: PolicyControls
    now: datetime


@dataclass(frozen=True, slots=True)
class Fired:
    rule_id: RuleId
    outcome: Outcome | PresendOutcome
    error: str | None = None

    @property
    def label(self) -> str:
        """Decision-log label; a rule that raised is logged as FAIL_CLOSED:<rule>."""
        return f"FAIL_CLOSED:{self.rule_id}" if self.error is not None else str(self.rule_id)


@dataclass(frozen=True, slots=True)
class ProposalResult:
    outcome: Outcome
    fired: tuple[Fired, ...]
    trip_breaker: bool
    policy_version: str = POLICY_VERSION

    @property
    def rule_ids(self) -> tuple[str, ...]:
        return tuple(f.label for f in self.fired)


@dataclass(frozen=True, slots=True)
class PresendResult:
    outcome: PresendOutcome
    fired: tuple[Fired, ...]
    policy_version: str = POLICY_VERSION

    @property
    def rule_ids(self) -> tuple[str, ...]:
        return tuple(f.label for f in self.fired)


# --- shared hard rules --------------------------------------------------------------
# Each check returns True when the rule PASSES. Raising means "cannot decide".


def _amount_positive(p: RefundProposal) -> bool:
    return p.amount_minor > 0


def _order_provenance(p: RefundProposal, order: OrderFacts, looked_up: frozenset[str]) -> bool:
    return p.order_id in looked_up and order.order_id == p.order_id


def _order_ownership(order: OrderFacts, session_user_id: str) -> bool:
    if not session_user_id:
        raise MissingInputError("session user is required")
    return order.owner_user_id == session_user_id


def _currency_match(p: RefundProposal, order: OrderFacts) -> bool:
    return p.currency == order.currency


def _order_status(order: OrderFacts) -> bool:
    return FulfillmentStatus(order.status) is FulfillmentStatus.DELIVERED


def _refund_window(order: OrderFacts, now: datetime, window: timedelta) -> bool:
    if FulfillmentStatus(order.status) is not FulfillmentStatus.DELIVERED:
        return False
    if order.delivered_at is None:
        raise MissingInputError("delivered order has no delivered_at")
    return now - order.delivered_at <= window


# --- evaluation helpers -------------------------------------------------------------

_Check = Callable[[], bool]


def _run_checks(
    checks: Sequence[tuple[RuleId, _Check, Outcome | PresendOutcome]],
    fail_closed: Outcome | PresendOutcome,
) -> list[Fired]:
    """Evaluate every check in isolation; one raising never hides the others."""
    fired: list[Fired] = []
    for rule_id, check, outcome_if_failed in checks:
        try:
            passed = check()
        except Exception as exc:  # fail closed: any error is a non-pass (ADR-009)
            fired.append(Fired(rule_id, fail_closed, error=f"{type(exc).__name__}: {exc}"))
            continue
        if not passed:
            fired.append(Fired(rule_id, outcome_if_failed))
    return fired


def _hard_checks(
    p: RefundProposal,
    *,
    order: OrderFacts,
    session_user_id: str,
    looked_up: frozenset[str],
    history: Sequence[RefundRecord],
    config: PolicyConfig,
    now: datetime,
    include_pending: bool,
    out_of_policy: Outcome | PresendOutcome,
) -> list[tuple[RuleId, _Check, Outcome | PresendOutcome]]:
    """R01-R06, R09, R10, R15: the rules checked at proposal and again before sending."""

    def order_cap() -> bool:
        used = used_on_order(
            history,
            order_id=order.order_id,
            exclude_action_id=p.action_id,
            include_pending=include_pending,
        )
        return used + p.amount_minor <= order.charged_minor

    def customer_limit() -> bool:
        used = used_by_customer(
            history,
            exclude_action_id=p.action_id,
            now=now,
            window=config.per_customer_window,
            include_pending=include_pending,
        )
        return used + p.amount_minor <= config.per_customer_limit_minor

    return [
        (RuleId.R01_AMOUNT_POSITIVE, lambda: _amount_positive(p), out_of_policy),
        (
            RuleId.R02_ORDER_PROVENANCE,
            lambda: _order_provenance(p, order, looked_up),
            out_of_policy,
        ),
        (
            RuleId.R03_ORDER_OWNERSHIP,
            lambda: _order_ownership(order, session_user_id),
            out_of_policy,
        ),
        (RuleId.R04_CURRENCY_MATCH, lambda: _currency_match(p, order), out_of_policy),
        (RuleId.R05_ORDER_CHARGED_CAP, order_cap, out_of_policy),
        (RuleId.R06_CUSTOMER_LIMIT, customer_limit, out_of_policy),
        (RuleId.R09_ORDER_STATUS, lambda: _order_status(order), out_of_policy),
        (RuleId.R10_NO_CHARGEBACK, lambda: not order.chargeback_open, out_of_policy),
        (RuleId.R15_SUPPORTED_CURRENCY, lambda: p.currency == config.currency, out_of_policy),
    ]


@dataclass
class _Phase1:
    fired: list[Fired] = field(default_factory=list)
    trip_breaker: bool = False


def evaluate_proposal(inputs: ProposalInputs, config: PolicyConfig) -> ProposalResult:
    """Proposal-time POLICY_CHECK (ADR-009): two phases, most severe outcome wins."""
    p, order, now = inputs.proposal, inputs.order, inputs.now
    phase1 = _Phase1()

    def budget() -> bool:
        used = policy_approved_in_window(
            inputs.history,
            exclude_action_id=p.action_id,
            now=now,
            window=config.per_customer_window,
        )
        return used + p.amount_minor <= config.auto_approve_budget_minor

    def breaker() -> bool:
        if inputs.controls.breaker_tripped:
            return False
        total = inputs.global_policy_approved_in_breaker_window_minor + p.amount_minor
        if total > config.breaker_threshold_minor:
            phase1.trip_breaker = True
            return False
        return True

    def sticky() -> bool:
        if inputs.has_unresolved_escalation:
            return False
        cooldown_start = now - config.decline_cooldown
        return not any(
            d.order_id == order.order_id and d.declined_at >= cooldown_start
            for d in inputs.declines
        )

    checks: list[tuple[RuleId, _Check, Outcome | PresendOutcome]] = [
        *_hard_checks(
            p,
            order=order,
            session_user_id=inputs.session_user_id,
            looked_up=inputs.looked_up_order_ids,
            history=inputs.history,
            config=config,
            now=now,
            include_pending=True,
            out_of_policy=Outcome.OUT_OF_POLICY,
        ),
        (
            RuleId.R07_AUTO_APPROVE_MAX,
            lambda: p.amount_minor <= config.auto_approve_max_minor,
            Outcome.REVIEW,
        ),
        (
            RuleId.R08_REFUND_WINDOW,
            lambda: _refund_window(order, now, config.refund_window),
            Outcome.OUT_OF_POLICY,
        ),
        (RuleId.R11_AUTO_APPROVE_BUDGET, budget, Outcome.REVIEW),
        (
            RuleId.R12_OTHER_ACTION_ON_ORDER,
            lambda: (
                not has_other_action_on_order(
                    inputs.history, order_id=order.order_id, exclude_action_id=p.action_id
                )
            ),
            Outcome.REVIEW,
        ),
        (RuleId.R13_CIRCUIT_BREAKER, breaker, Outcome.REVIEW),
        (
            RuleId.R17_DAILY_PROPOSAL_CAP,
            lambda: inputs.customer_proposals_last_24h < config.daily_proposal_cap,
            Outcome.OUT_OF_POLICY,
        ),
        (RuleId.R18_STICKY_ESCALATION, sticky, Outcome.REVIEW),
    ]
    phase1.fired = _run_checks(checks, Outcome.FAIL_CLOSED)
    outcome = _most_severe(phase1.fired, Outcome.AUTO)

    fired = list(phase1.fired)
    if outcome is Outcome.REVIEW:
        # Phase 2: queue-admission checks apply only to proposals headed for review.
        admission: list[tuple[RuleId, _Check, Outcome | PresendOutcome]] = [
            (
                RuleId.R16_PENDING_REVIEW_CAP,
                lambda: inputs.customer_pending_reviews < config.pending_review_cap,
                Outcome.OUT_OF_POLICY,
            ),
            (
                RuleId.R19_GLOBAL_REVIEW_QUEUE_CAP,
                lambda: inputs.global_pending_reviews < config.global_review_queue_cap,
                Outcome.OUT_OF_POLICY,
            ),
        ]
        fired.extend(_run_checks(admission, Outcome.FAIL_CLOSED))
        outcome = _most_severe(fired, Outcome.AUTO)

    # A proposal that can never be approved must not trip the system-wide breaker.
    trip = phase1.trip_breaker and outcome is Outcome.REVIEW
    return ProposalResult(outcome=outcome, fired=tuple(fired), trip_breaker=trip)


def evaluate_presend(inputs: PresendInputs, config: PolicyConfig) -> PresendResult:
    """The single post-approval check, before the first send (ADR-004 step 6).

    Uses ADR-004 capacity (no pending exposure) and the current policy version.
    """
    p = inputs.proposal

    def sends_not_held() -> bool:
        if not inputs.controls.sends_enabled:
            return False
        return not (inputs.approved_by_policy and inputs.controls.breaker_tripped)

    checks: list[tuple[RuleId, _Check, Outcome | PresendOutcome]] = [
        *_hard_checks(
            p,
            order=inputs.order,
            session_user_id=inputs.session_user_id,
            looked_up=inputs.looked_up_order_ids,
            history=inputs.history,
            config=config,
            now=inputs.now,
            include_pending=False,
            out_of_policy=PresendOutcome.OUT_OF_POLICY,
        ),
        (RuleId.R14_SENDS_HELD, sends_not_held, PresendOutcome.HOLD),
    ]
    fired = _run_checks(checks, PresendOutcome.FAIL_CLOSED)
    outcome = _most_severe(fired, PresendOutcome.PROCEED)
    return PresendResult(outcome=outcome, fired=tuple(fired))


def _most_severe[T: (Outcome, PresendOutcome)](fired: Sequence[Fired], default: T) -> T:
    result = default
    for f in fired:
        if isinstance(f.outcome, type(default)) and f.outcome > result:
            result = f.outcome
    return result
