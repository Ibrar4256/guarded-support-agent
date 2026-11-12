"""The refund policy's thresholds from the environment (ADR-009 Config: all from settings,
validated at startup). Shared by the worker and, later, the chat API. Durations are seconds,
like every other duration setting."""

from collections.abc import Mapping

from agent import envvars
from agent.core.policy import PolicyConfig

CURRENCY = "POLICY_CURRENCY"
AUTO_APPROVE_MAX = "POLICY_AUTO_APPROVE_MAX_MINOR"
AUTO_APPROVE_BUDGET = "POLICY_AUTO_APPROVE_BUDGET_MINOR"
PER_CUSTOMER_LIMIT = "POLICY_PER_CUSTOMER_LIMIT_MINOR"
PER_CUSTOMER_WINDOW = "POLICY_PER_CUSTOMER_WINDOW_SECONDS"
REFUND_WINDOW = "POLICY_REFUND_WINDOW_SECONDS"
BREAKER_THRESHOLD = "POLICY_BREAKER_THRESHOLD_MINOR"
PENDING_REVIEW_CAP = "POLICY_PENDING_REVIEW_CAP"
DAILY_PROPOSAL_CAP = "POLICY_DAILY_PROPOSAL_CAP"
GLOBAL_REVIEW_QUEUE_CAP = "POLICY_GLOBAL_REVIEW_QUEUE_CAP"
DECLINE_COOLDOWN = "POLICY_DECLINE_COOLDOWN_SECONDS"


class PolicySettingsError(RuntimeError):
    """A policy setting is missing or malformed; the service must not start."""


def policy_config_from_env(environ: Mapping[str, str]) -> PolicyConfig:
    """Read and validate; raises ``PolicySettingsError`` or the core's ``ConfigError``."""
    error = PolicySettingsError
    config = PolicyConfig(
        currency=envvars.required(environ, CURRENCY, error),
        auto_approve_max_minor=envvars.count(environ, AUTO_APPROVE_MAX, error),
        auto_approve_budget_minor=envvars.count(environ, AUTO_APPROVE_BUDGET, error),
        per_customer_limit_minor=envvars.count(environ, PER_CUSTOMER_LIMIT, error),
        per_customer_window=envvars.seconds(environ, PER_CUSTOMER_WINDOW, error),
        refund_window=envvars.seconds(environ, REFUND_WINDOW, error),
        breaker_threshold_minor=envvars.count(environ, BREAKER_THRESHOLD, error),
        pending_review_cap=envvars.count(environ, PENDING_REVIEW_CAP, error),
        daily_proposal_cap=envvars.count(environ, DAILY_PROPOSAL_CAP, error),
        global_review_queue_cap=envvars.count(environ, GLOBAL_REVIEW_QUEUE_CAP, error),
        decline_cooldown=envvars.seconds(environ, DECLINE_COOLDOWN, error),
    )
    config.validate()
    return config
