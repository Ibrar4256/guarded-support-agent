"""A complete, valid worker environment for tests, independent of any local ``.env``."""

from collections.abc import Mapping


def example_worker_env(
    *, db_url: str = "postgresql+psycopg://u:p@localhost:1/agent"
) -> dict[str, str]:
    return {
        "AGENT_DB_RUNTIME_URL": db_url,
        "WORKER_DB_POOL_SIZE": "4",
        "AGENT_DB_PREPARE_THRESHOLD": "",
        "ALERT_RATE_LIMIT_SECONDS": "300",
        "REFUND_API_URL": "http://127.0.0.1:9",
        "REFUND_API_TOKEN": "t" * 32,
        "REFUND_CLIENT_TIMEOUT_SECONDS": "1",
        "REFUND_CLIENT_MAX_ATTEMPTS": "3",
        "REFUND_CLIENT_BACKOFF_BASE_SECONDS": "0.1",
        "REFUND_CLIENT_BACKOFF_CAP_SECONDS": "0.5",
        "REFUND_CLIENT_MAX_PROVIDER_WINDOW_SECONDS": "900",
        "REFUND_CLIENT_MIN_PROVIDER_BUDGET_SECONDS": "1",
        "WORKER_LEASE_SECONDS": "6",
        "WORKER_HEARTBEAT_SECONDS": "2",
        "WORKER_LIVENESS_MARGIN_SECONDS": "0.5",
        "WORKER_CLAIM_COOLDOWN_SECONDS": "30",
        "WORKER_STATEMENT_TIMEOUT_SECONDS": "2",
        "WORKER_LOCK_TIMEOUT_SECONDS": "0.5",
        "WORKER_CONFIG_REFRESH_SECONDS": "30",
        "WORKER_CONFIG_STALE_SECONDS": "90",
        "WORKER_PRESEND_MAX_ATTEMPTS": "3",
        "WORKER_LOCK_RETRY_MAX": "3",
        "WORKER_RELEASE_STREAK_LIMIT": "5",
        "WORKER_MAX_FAILED_READS": "3",
        "WORKER_POLL_SECONDS": "0.2",
        "WORKER_SHUTDOWN_GRACE_SECONDS": "3",
        "RECONCILER_QUIET_MARGIN_SECONDS": "1",
        "RECONCILER_STALE_ALERT_SECONDS": "3600",
        "RECONCILER_STALE_PAGE_SECONDS": "7200",
        "PROVIDER_BREAKER_FAILURE_THRESHOLD": "2",
        "PROVIDER_BREAKER_OPEN_SECONDS": "5",
        "PROVIDER_BREAKER_MAX_OPEN_SECONDS": "60",
        "PROVIDER_BREAKER_PAGE_AFTER_SECONDS": "600",
        "POLICY_CURRENCY": "USD",
        "POLICY_AUTO_APPROVE_MAX_MINOR": "5000",
        "POLICY_AUTO_APPROVE_BUDGET_MINOR": "10000",
        "POLICY_PER_CUSTOMER_LIMIT_MINOR": "20000",
        "POLICY_PER_CUSTOMER_WINDOW_SECONDS": "2592000",
        "POLICY_REFUND_WINDOW_SECONDS": "2592000",
        "POLICY_BREAKER_WINDOW_SECONDS": "3600",
        "POLICY_BREAKER_THRESHOLD_MINOR": "100000",
        "POLICY_PENDING_REVIEW_CAP": "3",
        "POLICY_DAILY_PROPOSAL_CAP": "5",
        "POLICY_GLOBAL_REVIEW_QUEUE_CAP": "50",
        "POLICY_DECLINE_COOLDOWN_SECONDS": "604800",
    }


def example_turn_env(
    *, db_url: str = "postgresql+psycopg://u:p@localhost:1/agent"
) -> dict[str, str]:
    """The worker's environment plus the turn's settings (ADR-022 S2), the .env.example values."""
    return {
        **example_worker_env(db_url=db_url),
        "TURN_MAX_MODEL_CALLS": "6",
        "TURN_MAX_MALFORMED": "2",
        "TURN_DEADLINE_SECONDS": "60",
        "TURN_LEASE_SECONDS": "30",
        "TURN_HEARTBEAT_SECONDS": "10",
        "TURN_STATEMENT_TIMEOUT_SECONDS": "5",
        "TURN_LOCK_TIMEOUT_SECONDS": "2",
        "MEMORY_WINDOW_MESSAGES": "12",
        "DIGEST_TICKETS": "5",
        "CUSTOMER_MESSAGE_MAX_CHARS": "4000",
        "PROMPT_WINDOW_MAX_CHARS": "24000",
        "TURN_PROVIDER_FAILURE_ALERT_AFTER": "3",
        "SUMMARY_TRIGGER_MESSAGES": "8",
        "SUMMARY_DEADLINE_SECONDS": "30",
        "TURN_PROPOSAL_LOCK_ATTEMPTS": "3",
        "REVIEW_DEADLINE_SECONDS": "86400",
    }


# Every provider credential variable (ADR-025 S1); the real tools copy these from the
# developer's environment and nothing else.
CREDENTIAL_VARS = (
    "GROQ_API_KEY",
    "GEMINI_API_KEY",
    "CLOUDFLARE_API_TOKEN",
    "CLOUDFLARE_ACCOUNT_ID",
    "OPENROUTER_API_KEY",
    "SAMBANOVA_API_KEY",
    "OLLAMA_BASE_URL",
)


def example_llm_env(*, turn_models: str, summary_models: str) -> dict[str, str]:
    """The model settings with the .env.example values (ADR-023 S3, ADR-025 S1); no keys."""
    return {
        "LLM_TURN_MODELS": turn_models,
        "LLM_SUMMARY_MODELS": summary_models,
        "LLM_MAX_OUTPUT_TOKENS": "1024",
        "LLM_MAX_OUTPUT_CHARS": "16000",
        "LLM_TEMPERATURE": "0.2",
        "LLM_MAX_ATTEMPTS": "3",
        "LLM_BACKOFF_BASE_SECONDS": "0.5",
        "LLM_BACKOFF_CAP_SECONDS": "8",
        "LLM_MIN_ATTEMPT_SECONDS": "5",
        "LLM_QUOTA_COOLDOWN_SECONDS": "900",
    }


def credentials(environ: Mapping[str, str]) -> dict[str, str]:
    return {name: environ[name] for name in CREDENTIAL_VARS if environ.get(name)}
