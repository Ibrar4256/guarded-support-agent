"""The one timing profile both services run with in the crash tests (ADR-015).

Short enough that every wait stays bounded, and inside every startup guard:
- refund-api: retention (300 s) > window (60 s) + recovery delay (60 s); its pool timeout
  (1 s) < its processing budget (2 s).
- agent: worst case 3 x 2 s + 2 x 0.5 s = 7 s <= the 60 s window it reads, and the window
  <= its 900 s cap; retention > window + client timeout; lease (3 s) >= 3 heartbeats (1 s);
  statement timeout (1.5 s) < its step bound (2 + 0.5 + 0.5 + 0.5 = 3.5 s); shutdown grace
  (3 s) > client timeout (2 s).
``test_harness.py`` starts both services with it, so a profile a guard refuses fails there,
not in a crash test where it would look like a crash bug.
"""

CLIENT_TIMEOUT_SECONDS = 2.0
LEASE_SECONDS = 3.0
# timeout_after_commit hangs past the client timeout, so the agent sees no answer.
HANG_SECONDS = 4.0

REFUND_API = {
    "APP_ENV": "test",
    "DEBUG_FAULT_INJECTION": "true",
    "REFUND_API_TOKEN_NEXT": "",
    "REFUND_API_PROCESSING_BUDGET_SECONDS": "2",
    "REFUND_API_KEY_RETENTION_SECONDS": "300",
    "REFUND_API_MAX_RETRY_WINDOW_SECONDS": "60",
    "REFUND_API_MAX_RECOVERY_DELAY_SECONDS": "60",
    "REFUND_API_POOL_TIMEOUT_SECONDS": "1",
}

WORKER = {
    "APP_ENV": "test",
    "DEBUG_FAULT_INJECTION": "true",
    "WORKER_DB_POOL_SIZE": "4",
    "AGENT_DB_PREPARE_THRESHOLD": "",
    "ALERT_RATE_LIMIT_SECONDS": "300",
    "REFUND_CLIENT_TIMEOUT_SECONDS": str(CLIENT_TIMEOUT_SECONDS),
    "REFUND_CLIENT_MAX_ATTEMPTS": "3",
    "REFUND_CLIENT_BACKOFF_BASE_SECONDS": "0.1",
    "REFUND_CLIENT_BACKOFF_CAP_SECONDS": "0.5",
    "REFUND_CLIENT_MAX_PROVIDER_WINDOW_SECONDS": "900",
    "WORKER_LEASE_SECONDS": str(LEASE_SECONDS),
    "WORKER_HEARTBEAT_SECONDS": "1",
    "WORKER_LIVENESS_MARGIN_SECONDS": "0.5",
    "WORKER_CLAIM_COOLDOWN_SECONDS": "30",
    "WORKER_STATEMENT_TIMEOUT_SECONDS": "1.5",
    "WORKER_LOCK_TIMEOUT_SECONDS": "0.5",
    "WORKER_CONFIG_REFRESH_SECONDS": "30",
    "WORKER_CONFIG_STALE_SECONDS": "90",
    "WORKER_PRESEND_MAX_ATTEMPTS": "3",
    "WORKER_LOCK_RETRY_MAX": "3",
    "WORKER_RELEASE_STREAK_LIMIT": "5",
    "WORKER_MAX_FAILED_READS": "3",
    "WORKER_POLL_SECONDS": "0.2",
    "WORKER_SHUTDOWN_GRACE_SECONDS": "3",
    "POLICY_CURRENCY": "USD",
    "POLICY_AUTO_APPROVE_MAX_MINOR": "5000",
    "POLICY_AUTO_APPROVE_BUDGET_MINOR": "10000",
    "POLICY_PER_CUSTOMER_LIMIT_MINOR": "20000",
    "POLICY_PER_CUSTOMER_WINDOW_SECONDS": "2592000",
    "POLICY_REFUND_WINDOW_SECONDS": "2592000",
    "POLICY_BREAKER_THRESHOLD_MINOR": "100000",
    "POLICY_PENDING_REVIEW_CAP": "3",
    "POLICY_DAILY_PROPOSAL_CAP": "5",
    "POLICY_GLOBAL_REVIEW_QUEUE_CAP": "50",
    "POLICY_DECLINE_COOLDOWN_SECONDS": "604800",
}
