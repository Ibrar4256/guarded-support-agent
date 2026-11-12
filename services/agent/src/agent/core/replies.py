"""Customer reply templates after approval (ADR-001, ADR-008 "Customer reply templates").

Once a run is approved, the model no longer writes replies: the worker appends an event
naming one of these templates, and the chat API renders its text (ADR-014 S10). Templates
never name rule IDs, thresholds or limits (ADR-009 non-disclosure).
"""

from enum import StrEnum


class ReplyTemplate(StrEnum):
    REFUND_ISSUED = "refund_issued"  # COMPLETED / confirmed
    REVIEW_DECLINED = "review_declined"  # COMPLETED / none
    NEEDS_PERSON = "needs_person"  # ESCALATED before any money moved
    NOT_CONFIRMED = "not_confirmed"  # ESCALATED / rejected by the reconciler
    PROCESSING = "processing"  # EXECUTING or NEEDS_RECONCILIATION
