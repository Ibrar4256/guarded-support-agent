"""The agent's side of the refund API (ADR-004, ADR-007, ADR-012): its own models, its own
copy of the canonical payload hash, and an HTTP client that makes one attempt per call.
Classification of what comes back is pure code in ``agent.core.refund_outcome``."""
